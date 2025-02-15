"""Add text to an image."""
from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QSizeF, QSize, QRect
from PySide6.QtGui import QIcon, QMouseEvent, Qt, QTransform, QCursor
from PySide6.QtWidgets import QWidget, QApplication

from src.config.cache import Cache
from src.config.key_config import KeyConfig
from src.hotkey_filter import HotkeyFilter
from src.image.layers.image_stack import ImageStack
from src.image.layers.image_stack_utils import top_layer_at_point
from src.image.layers.layer import Layer
from src.image.layers.text_layer import TextLayer
from src.image.layers.transform_layer import TransformLayer
from src.image.text_rect import TextRect
from src.tools.base_tool import BaseTool
from src.ui.graphics_items.click_and_drag_selection import ClickAndDragSelection
from src.ui.graphics_items.layer_lock_alert_item import LayerLockAlertItem
from src.ui.graphics_items.placement_outline import PlacementOutline
from src.ui.image_viewer import ImageViewer
from src.ui.panel.tool_control_panels.text_tool_panel import TextToolPanel
from src.undo_stack import UndoStack
from src.util.shared_constants import PROJECT_DIR
from src.util.visual.text_drawing_utils import left_button_hint_text

# The `QCoreApplication.translate` context for strings in this file
TR_ID = 'tools.text_tool'


def _tr(*args):
    """Helper to make `QCoreApplication.translate` more concise."""
    return QApplication.translate(TR_ID, *args)


ICON_PATH_TEXT_TOOL = f'{PROJECT_DIR}/resources/icons/tools/text_icon.svg'
MIN_DRAG_SIZE = 4

TEXT_LABEL = _tr('Text')
TEXT_TOOLTIP = _tr('Add text to a text layer')
TEXT_CONTROL_HINT = _tr('{left_mouse_icon}: select text layer<br/>{left_mouse_icon}, drag:'
                        ' create new layer or move active')


class TextTool(BaseTool):
    """Lets the user fill image areas with solid colors."""

    def __init__(self, image_stack: ImageStack, image_viewer: ImageViewer) -> None:
        super().__init__(KeyConfig.TEXT_TOOL_KEY, TEXT_LABEL, TEXT_TOOLTIP, QIcon(ICON_PATH_TEXT_TOOL))
        self.cursor = QCursor(Qt.CursorShape.CrossCursor)
        scene = image_viewer.scene()
        assert scene is not None
        self._scene = scene
        self._control_panel = TextToolPanel()
        self._image_stack = image_stack
        self._image_viewer = image_viewer
        self._placement_outline = PlacementOutline(QPointF(), QSizeF())
        scene.addItem(self._placement_outline)
        self._placement_outline.setVisible(False)
        self._text_layer: Optional[TextLayer] = None
        self._selection_handler = ClickAndDragSelection(scene)
        self._dragging = False
        self._image_stack.active_layer_changed.connect(self._active_layer_change_slot)

        def _use_fixed_aspect_ratio(modifiers: Qt.KeyboardModifier) -> None:
            self._placement_outline.preserve_aspect_ratio = KeyConfig.modifier_held(KeyConfig.FIXED_ASPECT_MODIFIER,
                                                                                    held_modifiers=modifiers)
        HotkeyFilter.instance().modifiers_changed.connect(_use_fixed_aspect_ratio)

    def get_input_hint(self) -> str:
        """Return text describing different input functionality."""
        text_hint = TEXT_CONTROL_HINT.format(left_mouse_icon=left_button_hint_text())
        return (f'{text_hint}<br/>{BaseTool.fixed_aspect_hint()}'
                f'<br/>{super().get_input_hint()}')

    def get_control_panel(self) -> Optional[QWidget]:
        """Returns a panel providing controls for customizing tool behavior, or None if no such panel is needed."""
        return self._control_panel

    def _active_layer_mouse_bounds(self) -> QRect:
        if self._text_layer is None:
            return QRect()
        return self._text_layer.transformed_bounds.adjusted(-10, -10, 10, 10)

    def mouse_click(self, event: Optional[QMouseEvent], image_coordinates: QPoint) -> bool:
        """Updates text placement or selects a text layer on click."""
        assert event is not None
        if self._text_layer is not None:
            active_bounds = self._active_layer_mouse_bounds()
            if active_bounds.contains(image_coordinates):
                return False
        if event.buttons() == Qt.MouseButton.LeftButton:
            clicked_layer = top_layer_at_point(self._image_stack, image_coordinates)
            if isinstance(clicked_layer, TextLayer):
                self._connect_text_layer(clicked_layer)
                if clicked_layer.locked or clicked_layer.parent_locked:
                    LayerLockAlertItem(clicked_layer, self._image_viewer)
                return True
            self._dragging = True
            self._selection_handler.start_selection(image_coordinates)
        return False

    def mouse_move(self, event: Optional[QMouseEvent], image_coordinates: QPoint) -> bool:
        """Updates text placement while dragging when a text layer is active."""
        assert event is not None
        if self._text_layer is not None:
            active_bounds = self._active_layer_mouse_bounds()
            if active_bounds.contains(image_coordinates) and not self._dragging:
                return False
        if event.buttons() == Qt.MouseButton.LeftButton and self._dragging:
            self._selection_handler.drag_to(image_coordinates)
            return True
        if self._dragging:
            self._selection_handler.end_selection(image_coordinates)
            self._dragging = False
            return True
        return False

    def mouse_release(self, event: Optional[QMouseEvent], image_coordinates: QPoint) -> bool:
        """If dragging, finish and create a new text layer."""
        if self._dragging:
            new_bounds = self._selection_handler.end_selection(image_coordinates).boundingRect().toAlignedRect()
            self._dragging = False
            if new_bounds.width() > MIN_DRAG_SIZE or new_bounds.height() > MIN_DRAG_SIZE:
                if self._text_layer is not None:
                    self._disconnect_text_layer()
                self._control_panel.offset = new_bounds.topLeft()
                text_rect = self._control_panel.text_rect
                text_rect.size = new_bounds.size()
                self._control_panel.text_rect = text_rect
                self._create_and_activate_text_layer(text_rect, new_bounds.topLeft())
            return True
        return False

    def _on_activate(self, restoring_after_delegation=False) -> None:
        """Called when the tool becomes active, implement to handle any setup that needs to be done."""
        active_layer = self._image_stack.active_layer
        if isinstance(active_layer, TextLayer):
            self._connect_text_layer(active_layer)
            if restoring_after_delegation:
                text_rect = active_layer.text_rect
                text_rect.text_color = Cache().get_color(Cache.LAST_BRUSH_COLOR, Qt.GlobalColor.black)
                text_rect.background_color = Cache().get_color(Cache.TEXT_BACKGROUND_COLOR, Qt.GlobalColor.white)
                self._control_panel.text_rect = text_rect
                active_layer.text_rect = text_rect

    def _on_deactivate(self) -> None:
        """Called when the tool stops being active, implement to handle any cleanup that needs to be done."""
        if self._text_layer is not None:
            self._disconnect_text_layer()
        if self._dragging:
            self._selection_handler.end_selection(QPoint())
            self._dragging = False

    def _connect_text_layer(self, layer: TextLayer) -> None:
        if self._text_layer is not None:
            self._disconnect_text_layer()
        self._text_layer = layer
        if self._image_stack.active_layer != layer:
            self._image_stack.active_layer = layer
        text_rect = layer.text_rect
        self._control_panel.text_rect = text_rect
        self._control_panel.offset = layer.offset.toPoint()
        self._placement_outline.offset = layer.offset
        self._placement_outline.outline_size = QSizeF(text_rect.size)
        self._placement_outline.setTransform(layer.transform)
        self._placement_outline.setZValue(self._image_stack.selection_layer.z_value + 1)
        self._placement_outline.setVisible(True)
        self._connect_signals()
        self._control_panel.focus_text_input()
        self._layer_lock_change_slot(layer, layer.locked)

    def _disconnect_text_layer(self) -> None:
        if self._text_layer is not None:
            self._disconnect_signals()
            self._text_layer = None
            text_rect = self._control_panel.text_rect
            text_rect.text = ''
            self._control_panel.text_rect = text_rect
            self._placement_outline.setVisible(False)

    def _disconnect_signals(self) -> None:
        if self._text_layer is not None:
            self._text_layer.lock_changed.disconnect(self._layer_lock_change_slot)
            self._text_layer.transform_changed.disconnect(self._layer_transform_change_slot)
            self._text_layer.size_changed.disconnect(self._layer_size_change_slot)
        self._control_panel.text_rect_changed.disconnect(self._control_text_data_changed_slot)
        self._control_panel.offset_changed.disconnect(self._control_offset_changed_slot)
        self._placement_outline.placement_changed.disconnect(self._placement_outline_changed_slot)

    def _connect_signals(self):
        if self._text_layer is not None:
            self._text_layer.lock_changed.connect(self._layer_lock_change_slot)
            self._text_layer.transform_changed.connect(self._layer_transform_change_slot)
            self._text_layer.size_changed.connect(self._layer_size_change_slot)
        self._control_panel.text_rect_changed.connect(self._control_text_data_changed_slot)
        self._control_panel.offset_changed.connect(self._control_offset_changed_slot)
        self._placement_outline.placement_changed.connect(self._placement_outline_changed_slot)

    def _create_and_activate_text_layer(self, layer_data: Optional[TextRect], offset: QPoint) -> None:
        with UndoStack().combining_actions('TextTool._create_and_activate_text_layer'):
            text_layer = self._image_stack.create_text_layer(layer_data)
            text_layer.transform = QTransform.fromTranslate(offset.x(), offset.y())
            self._connect_text_layer(text_layer)

    def _control_text_data_changed_slot(self, text_data: TextRect) -> None:
        if not self.is_active:
            return
        if self._text_layer is not None:
            self._disconnect_signals()
            self._text_layer.text_rect = text_data
            self._placement_outline.outline_size = text_data.size
            self._connect_signals()

    def _control_offset_changed_slot(self, offset: QPoint) -> None:
        if self._text_layer is not None and self.is_active:
            self._disconnect_signals()
            if offset != self._text_layer.offset.toPoint():
                self._text_layer.offset = offset
            self._placement_outline.setTransform(self._text_layer.transform)
            self._connect_signals()

    def _placement_outline_changed_slot(self, offset: QPointF, size: QSizeF) -> None:
        if not self.is_active:
            return
        assert self._text_layer is not None
        self._disconnect_signals()
        if offset != self._text_layer.offset:
            self._text_layer.offset = offset
            self._control_panel.offset = offset.toPoint()
        text_rect = self._control_panel.text_rect
        if text_rect.size != size.toSize():
            text_rect.scale_bounds_to_text = False
            text_rect.size = size.toSize()
            self._control_panel.text_rect = text_rect
            self._text_layer.text_rect = text_rect

            assert self._control_panel.text_rect.size == size.toSize()
            assert self._text_layer.text_rect.size == size.toSize()
        self._connect_signals()

    def _layer_size_change_slot(self, layer: Layer, size: QSize) -> None:
        if layer != self._text_layer:
            layer.size_changed.disconnect(self._layer_size_change_slot)
            return
        if not self.is_active:
            return
        assert isinstance(layer, TextLayer)
        self._disconnect_signals()
        text_rect = self._control_panel.text_rect
        text_rect.size = size
        self._control_panel.text_rect = text_rect
        self._placement_outline.outline_size = QSizeF(size)
        self._connect_signals()

    def _layer_transform_change_slot(self, layer: TransformLayer, transform: QTransform) -> None:
        if layer != self._text_layer:
            layer.transform_changed.disconnect(self._layer_transform_change_slot)
            return
        if not self.is_active:
            return
        assert isinstance(layer, TextLayer)
        self._disconnect_signals()
        self._control_panel.offset = layer.offset.toPoint()
        self._placement_outline.setTransform(transform)
        self._connect_signals()

    # noinspection PyUnusedLocal
    def _layer_lock_change_slot(self, layer: Layer, locked: bool) -> None:
        if not self.is_active:
            return
        should_enable = self._text_layer is None or (not self._text_layer.locked and not self._text_layer.parent_locked)
        self._control_panel.setEnabled(should_enable)
        self._placement_outline.setEnabled(should_enable)
        self.set_disabled_cursor(not should_enable)

    def _active_layer_change_slot(self, active_layer: Layer) -> None:
        if active_layer == self._text_layer or not self.is_active:
            return
        if isinstance(active_layer, TextLayer):
            assert self.is_active
            self._connect_text_layer(active_layer)
        else:
            self._disconnect_text_layer()
