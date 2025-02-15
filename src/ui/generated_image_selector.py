"""
Provides an interface for choosing between AI-generated changes to selected image content.
"""
import math
import sys
import time
from typing import Callable, Optional, cast

from PIL import Image
from PySide6.QtCore import Qt, QRect, QSize, QSizeF, QRectF, QEvent, Signal, QPointF, QObject, QPoint
from PySide6.QtGui import QImage, QResizeEvent, QPixmap, QPainter, QWheelEvent, QMouseEvent, \
    QPainterPath, QKeyEvent, QPolygonF, QSinglePointEvent, QAction
from PySide6.QtWidgets import QApplication, QMenu
from PySide6.QtWidgets import QWidget, QGraphicsPixmapItem, QVBoxLayout, QLabel, \
    QStyleOptionGraphicsItem, QHBoxLayout, QPushButton, QStyle

from src.config.application_config import AppConfig
from src.config.cache import Cache
from src.config.key_config import KeyConfig
from src.image.layers.image_layer import ImageLayer
from src.image.layers.image_stack import ImageStack
from src.image.layers.layer import Layer
from src.ui.graphics_items.outline import Outline
from src.ui.graphics_items.polygon_outline import PolygonOutline
from src.ui.graphics_items.toast_message import ToastMessageItem
from src.ui.input_fields.check_box import CheckBox
from src.ui.modal.modal_utils import open_image_file, SAVE_IMAGE_MODE, show_warning_dialog
from src.ui.widget.image_graphics_view import ImageGraphicsView
from src.util.application_state import AppStateTracker, APP_STATE_LOADING, APP_STATE_EDITING
from src.util.math_utils import clamp
from src.util.shared_constants import TIMELAPSE_MODE_FLAG, EDIT_MODE_INPAINT
from src.util.validation import assert_valid_index
from src.util.visual.geometry_utils import get_scaled_placement
from src.util.visual.image_format_utils import save_image
from src.util.visual.image_utils import get_standard_qt_icon, get_transparency_tile_pixmap
from src.util.visual.pil_image_utils import pil_image_to_qimage, pil_image_scaling
from src.util.visual.text_drawing_utils import max_font_size, get_key_display_string, left_button_hint_text, \
    middle_button_hint_text, vertical_scroll_hint_text

# The `QCoreApplication.translate` context for strings in this file
TR_ID = 'ui.generated_image_selector'


def _tr(*args):
    """Helper to make `QCoreApplication.translate` more concise."""
    return QApplication.translate(TR_ID, *args)


CHANGE_ZOOM_CHECKBOX_LABEL = _tr('Zoom to changes')
SHOW_SELECTION_OUTLINES_LABEL = _tr('Show selection')
MODE_INPAINT = _tr('Inpaint')
CANCEL_BUTTON_TEXT = _tr('Cancel')
CANCEL_BUTTON_TOOLTIP = _tr('This will discard all generated images.')
PREVIOUS_BUTTON_TEXT = _tr('Previous')
ZOOM_BUTTON_TEXT = _tr('Toggle zoom')
NEXT_BUTTON_TEXT = _tr('Next')

ORIGINAL_CONTENT_LABEL = _tr('Original image content')
LABEL_TEXT_IMAGE_OPTION = _tr('Option {index}')
LOADING_IMG_TEXT = _tr('Loading...')

MENU_ACTION_SELECT = _tr('Select this option')
MENU_ACTION_SAVE_TO_FILE = _tr('Save to new file')
MENU_ACTION_SEND_TO_NEW_LAYER = _tr('Send to new layer')

TOAST_MESSAGE_SAVED = _tr('Saved image option to {image_path}')
TOAST_MESSAGE_LAYER_CREATED = _tr('Created new layer "{layer_name}"')
TOAST_MESSAGE_SAVE_CANCELED = _tr('Cancelled saving image option to file.')

WARNING_TITLE_INSERT_FAILED = _tr('Failed to insert into layer "{layer_name}"')
WARNING_MESSAGE_1_LAYER_LOCKED = _tr('Changes were blocked because the layer is locked. ')
WARNING_MESSAGE_1_LAYER_HIDDEN = _tr('Changes were blocked because the layer is hidden. ')
WARNING_MESSAGE_1_NOT_IMAGE = _tr('The active layer is not an image layer, image content cannot be inserted. ')
WARNING_MESSAGE_2_CREATED_NEW = _tr('Image content was added as new layer "{new_layer_name}"')

SELECTION_TITLE = _tr('Select from generated image options.')
VIEW_MARGIN = 6
IMAGE_MARGIN_FRACTION = 1 / 6
SCROLL_DEBOUNCE_MS = 100

# TODO: Using <pre> tags to force spacing is not ideal, figure out why Qt rich text table spacing properties don't work
DEFAULT_CONTROL_HINT = _tr("""
                           <table>
                             <tr>
                               <td>
                                 {modifier_or_modifiers}+{left_mouse_icon} or {middle_mouse_icon} and drag: pan view
                               </td>
                               <td><pre>    </pre></td>
                               <td>
                                 {v_scroll_icon} or {zoom_in_hint}/{zoom_out_hint}: zoom
                               </td>
                             </tr>
                             <tr>
                               <td>
                                 {up_key_hint}: zoom to first option
                               </td>
                               <td><pre>    </pre></td>
                               <td>
                                 {escape_key_hint}: discard all options
                               </td>
                              </tr>
                            </table>
                           """)
ZOOM_CONTROL_HINT = _tr("""
                           <table>
                             <tr>
                               <td>
                                 {modifier_or_modifiers}+{left_mouse_icon} or {middle_mouse_icon} and drag: pan view
                               </td>
                               <td><pre>    </pre></td>
                               <td>
                                 {v_scroll_icon} or {zoom_in_hint}/{zoom_out_hint}: zoom
                               </td>
                             </tr>
                             <tr>
                               <td>
                                 {enter_key_hint}: select option
                               </td>
                               <td><pre>    </pre></td>
                               <td>
                                 {escape_key_hint}/{down_key_hint}: return to full view
                               </td>
                              </tr>
                            </table>
                           """)

VIEW_BACKGROUND = Qt.GlobalColor.black


class GeneratedImageSelector(QWidget):
    """Shows all images from an image generation operation, allows the user to select one or discard all of them."""

    cancel_generation = Signal()

    def __init__(self,
                 image_stack: ImageStack,
                 close_selector: Callable) -> None:
        super().__init__(None)
        self._image_stack = image_stack
        self._generation_area = image_stack.generation_area
        self._close_selector = close_selector
        self._options: list[_ImageOption] = []
        self._outlines: list[Outline] = []
        self._selections: list[PolygonOutline] = []
        self._loading_image = QImage()
        self._zoomed_in = False
        self._zoom_to_changes = AppConfig().get(AppConfig.SELECTION_SCREEN_ZOOMS_TO_CHANGED)
        self._change_bounds: Optional[QRect] = None
        self._zoom_index = 0
        self._last_scroll_time = time.time() * 1000
        self._toast_message: Optional[ToastMessageItem] = None

        self._base_option_offset = QPointF(0.0, 0.0)
        self._base_option_scale = 0.0
        self._option_scale_offset = 0.0
        self._option_pos_offset = QPointF(0.0, 0.0)

        self._layout = QVBoxLayout(self)
        self._page_top_bar = QWidget(self)
        self._page_top_layout = QHBoxLayout(self._page_top_bar)
        self._page_top_label = QLabel(SELECTION_TITLE)
        self._page_top_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_top_layout.addWidget(self._page_top_label, stretch=255)
        self._layout.addWidget(self._page_top_bar)

        # Setup main option view widget:
        self._view = _SelectionView()
        self._view.scale_changed.connect(self._scale_change_slot)
        self._view.offset_changed.connect(self._offset_change_slot)
        self._view.setMouseTracking(True)
        self._view.installEventFilter(self)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        config = AppConfig()

        def _selection_scroll(dx, dy):
            if dx > 0:
                self._zoom_next()
            elif dx < 0:
                self._zoom_prev()
            if dy != 0 and (dy > 0) == self._zoomed_in:
                self.toggle_zoom()

        self._view.content_scrolled.connect(_selection_scroll)
        self._view.zoom_toggled.connect(self.toggle_zoom)

        self._layout.addWidget(self._view, stretch=255)

        # Add inpainting checkboxes:
        # show/hide selection outlines:
        self._selection_outline_checkbox = config.get_control_widget(
            AppConfig.SHOW_SELECTIONS_IN_GENERATION_OPTIONS)
        assert isinstance(self._selection_outline_checkbox, CheckBox)
        self._selection_outline_checkbox.setText(SHOW_SELECTION_OUTLINES_LABEL)
        self._selection_outline_checkbox.toggled.connect(self.set_selection_outline_visibility)
        self._page_top_layout.addWidget(self._selection_outline_checkbox)

        # zoom to changed area:
        self._change_zoom_checkbox = config.get_control_widget(AppConfig.SELECTION_SCREEN_ZOOMS_TO_CHANGED)
        assert isinstance(self._change_zoom_checkbox, CheckBox)
        self._change_zoom_checkbox.setText(CHANGE_ZOOM_CHECKBOX_LABEL)
        self._change_zoom_checkbox.toggled.connect(self.zoom_to_changes)
        self._page_top_layout.addWidget(self._change_zoom_checkbox)

        self._button_bar = QWidget(self)
        self._layout.addWidget(self._button_bar)
        self._button_bar_layout = QHBoxLayout(self._button_bar)

        self._cancel_button = QPushButton()
        self._cancel_button.setIcon(get_standard_qt_icon(QStyle.StandardPixmap.SP_DialogCancelButton))
        self._cancel_button.setText(CANCEL_BUTTON_TEXT)
        self._cancel_button.setToolTip(CANCEL_BUTTON_TOOLTIP)

        def _cancel() -> None:
            if AppStateTracker.app_state() == APP_STATE_LOADING:
                self.cancel_generation.emit()
            self._close_selector()

        self._cancel_button.clicked.connect(_cancel)
        self._button_bar_layout.addWidget(self._cancel_button)

        self._button_bar_layout.addStretch(255)

        self._status_label = QLabel(self._get_control_hint())
        self._button_bar_layout.addWidget(self._status_label)
        self._button_bar_layout.addStretch(255)

        key_config = KeyConfig()

        def _add_key_hint(button, config_key):
            keys = key_config.get_keycodes(config_key)
            button.setText(f'{button.text()} {get_key_display_string(keys, rich_text=False)}')

        self._prev_button = QPushButton()
        self._prev_button.setIcon(get_standard_qt_icon(QStyle.StandardPixmap.SP_ArrowLeft))
        self._prev_button.setText(PREVIOUS_BUTTON_TEXT)
        self._prev_button.clicked.connect(self._zoom_prev)
        _add_key_hint(self._prev_button, KeyConfig.MOVE_LEFT)
        self._button_bar_layout.addWidget(self._prev_button)

        self._zoom_button = QPushButton()
        self._zoom_button.setText(ZOOM_BUTTON_TEXT)
        self._zoom_button.clicked.connect(self.toggle_zoom)
        _add_key_hint(self._zoom_button, KeyConfig.ZOOM_TOGGLE)
        self._button_bar_layout.addWidget(self._zoom_button)

        self._next_button = QPushButton()
        self._next_button.setIcon(get_standard_qt_icon(QStyle.StandardPixmap.SP_ArrowRight))
        self._next_button.setText(NEXT_BUTTON_TEXT)
        self._next_button.clicked.connect(self._zoom_next)
        _add_key_hint(self._next_button, KeyConfig.MOVE_RIGHT)
        self._button_bar_layout.addWidget(self._next_button)
        self.reset()

    def reset(self) -> None:
        """Remove all old options and prepare for new ones."""
        self._generation_area = self._image_stack.generation_area
        self._zoom_index = 0
        cache = Cache()
        scene = self._view.scene()
        assert scene is not None
        # Clear the scene:
        for scene_item_list in (self._selections, self._outlines, self._options):
            assert isinstance(scene_item_list, list)
            while len(scene_item_list) > 0:
                scene_item = scene_item_list.pop()
                if hasattr(scene_item, 'animated'):
                    scene_item.animated = False
                if scene_item in scene.items():
                    scene.removeItem(scene_item)

        # Configure checkboxes and change bounds:
        if cache.get(Cache.EDIT_MODE) == MODE_INPAINT:
            self._selection_outline_checkbox.setVisible(True)
            change_bounds = self._image_stack.selection_layer.get_selection_gen_area(True)
            if change_bounds != self._image_stack.generation_area and change_bounds is not None:
                change_bounds.translate(-self._image_stack.generation_area.x(), -self._image_stack.generation_area.y())
                self._change_bounds = change_bounds
                self._change_zoom_checkbox.setVisible(True)
            else:
                self._change_bounds = None
                self._change_zoom_checkbox.setVisible(False)
        else:
            self._selection_outline_checkbox.setVisible(False)
            self._change_zoom_checkbox.setVisible(False)
            self._change_bounds = None

        # Add initial images, placeholders for expected images:
        original_image = self._image_stack.qimage_generation_area_content()
        original_option = _ImageOption(original_image, ORIGINAL_CONTENT_LABEL)
        scene.addItem(original_option)
        self._options.append(original_option)
        self._outlines.append(Outline(scene, self._view))
        self._outlines[0].outlined_region = self._options[0].bounds
        if Cache().get(Cache.EDIT_MODE) == MODE_INPAINT:
            self._add_option_selection_outline(0)

        self._loading_image = QImage(original_image.size(), QImage.Format.Format_ARGB32_Premultiplied)
        self._loading_image.fill(Qt.GlobalColor.black)
        painter = QPainter(self._loading_image)
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(QRect(0, 0, self._loading_image.width(), self._loading_image.height()),
                         Qt.AlignmentFlag.AlignCenter,
                         LOADING_IMG_TEXT)
        painter.end()

        expected_count = cache.get(Cache.BATCH_SIZE) * cache.get(Cache.BATCH_COUNT)
        for i in range(expected_count):
            self.add_image_option(self._loading_image, i)

        if self._zoomed_in and TIMELAPSE_MODE_FLAG not in sys.argv:
            self.toggle_zoom()
            self._view.reset_scale()
        elif TIMELAPSE_MODE_FLAG in sys.argv:
            self._zoom_to_option(0)
        self._apply_ideal_image_arrangement()

    def _add_option_selection_outline(self, idx: int) -> None:
        if len(self._options) <= idx:
            raise IndexError(f'Invalid option index {idx}')
        if len(self._selections) != idx:
            raise RuntimeError(f'Generating selection outline {idx}, unexpected outline count {len(self._selections)}'
                               f' found.')
        selection_crop = QPolygonF(QRectF(self._image_stack.generation_area))
        origin = self._image_stack.generation_area.topLeft()
        selection_polys = (poly.intersected(selection_crop).translated(-origin.x(), -origin.y())
                           for poly in self._image_stack.selection_layer.outline)
        polys = [QPolygonF(poly) for poly in selection_polys]
        outline = PolygonOutline(self._view, polys)
        outline.animated = AppConfig().get(AppConfig.ANIMATE_OUTLINES)
        outline.setScale(self._image_stack.width / self._image_stack.generation_area.width())
        outline.setVisible(AppConfig().get(AppConfig.SHOW_SELECTIONS_IN_GENERATION_OPTIONS))
        self._selections.append(outline)

    def add_image_option(self, image: QImage, idx: int) -> None:
        """Add an image to the list of generated image options."""
        if not 0 <= idx < len(self._options):
            raise IndexError(f'invalid index {idx}, max is {len(self._options)}')
        idx += 1  # Original image gets index zero
        if idx == len(self._options):
            self._options.append(_ImageOption(image, LABEL_TEXT_IMAGE_OPTION.format(index=idx)))
            scene = self._view.scene()
            assert scene is not None, 'Scene should have been created automatically and never cleared'
            scene.addItem(self._options[-1])
            self._outlines.append(Outline(scene, self._view))
            # Add selections if inpainting:
            if Cache().get(Cache.EDIT_MODE) == MODE_INPAINT:
                self._add_option_selection_outline(idx)
        else:
            self._options[idx].image = image
        if 0 <= idx < len(self._outlines):
            self._outlines[idx].outlined_region = self._options[idx].bounds
        self._apply_ideal_image_arrangement()

    def toggle_zoom(self, zoom_index: Optional[int] = None) -> None:
        """Toggle between zooming in on one option and showing all of them."""
        if zoom_index is not None:
            self._zoom_index = zoom_index
        self._zoomed_in = not self._zoomed_in
        if self._zoomed_in:
            self._zoom_to_option(self._zoom_index, True)
        else:
            if not self._scroll_debounce_finished():
                return
            self._view.reset_scale()
            self._option_pos_offset = QPointF(0.0, 0.0)
            self._option_scale_offset = 0.0
            for option in self._options:
                option.setOpacity(1.0)
            self._status_label.setText(self._get_control_hint())
            self._page_top_label.setText(SELECTION_TITLE)
        self.resizeEvent(None)

    def zoom_to_changes(self, should_zoom: bool) -> None:
        """Zoom in to the updated area when inpainting small sections."""
        self._zoom_to_changes = should_zoom
        if self._zoom_to_changes:
            if not self._zoomed_in:
                self.toggle_zoom()
            self._zoom_to_option(self._zoom_index, True)
        elif self._zoomed_in:
            self._zoom_to_option(self._zoom_index, True)

    def set_selection_outline_visibility(self, show_selections: bool) -> None:
        """Set whether selection outlines are drawn."""
        for selection_outline in self._selections:
            selection_outline.setVisible(show_selections)

    def resizeEvent(self, unused_event: Optional[QResizeEvent]):
        """Recalculate all bounds on resize and update view scale."""
        self._apply_ideal_image_arrangement()
        if self._zoomed_in:
            self._zoom_to_option(self._zoom_index, True)

    def _show_context_menu(self, idx: int, pos: QPoint) -> None:
        menu = QMenu()
        menu.setTitle(ORIGINAL_CONTENT_LABEL if idx == 0 else LABEL_TEXT_IMAGE_OPTION.format(index=idx))

        def _add_action(name: str, action_callback: Callable[..., None]) -> QAction:
            action = menu.addAction(name)
            assert action is not None
            action.triggered.connect(action_callback)
            return action
        _add_action(MENU_ACTION_SELECT, lambda: self._select_option_and_close(idx))

        def _save_to_file() -> None:
            image = self._options[idx].full_image
            self.setUpdatesEnabled(False)
            save_path = open_image_file(self, SAVE_IMAGE_MODE)
            self.setUpdatesEnabled(True)
            if save_path is not None:
                save_image(image, save_path)
                self._toast_message = ToastMessageItem(TOAST_MESSAGE_SAVED.format(image_path=save_path), self._view)
            else:
                self._toast_message = ToastMessageItem(TOAST_MESSAGE_SAVE_CANCELED, self._view)
        _add_action(MENU_ACTION_SAVE_TO_FILE, _save_to_file)

        def _send_to_new_layer() -> None:
            new_layer = self._image_stack.create_layer(layer_name=menu.title())
            self._insert_option_into_layer(idx, new_layer)
            self._toast_message = ToastMessageItem(TOAST_MESSAGE_LAYER_CREATED.format(layer_name=new_layer.name),
                                                   self._view)
        _add_action(MENU_ACTION_SEND_TO_NEW_LAYER, _send_to_new_layer)
        menu.exec(self.mapToGlobal(pos))

    def eventFilter(self, source: Optional[QObject], event: Optional[QEvent]):
        """Use horizontal scroll to move through selections, select items when clicked."""
        assert event is not None
        if event.type() == QEvent.Type.Wheel:
            event = cast(QWheelEvent, event)
            if event.angleDelta().x() > 0:
                self._zoom_next()
            elif event.angleDelta().x() < 0:
                self._zoom_prev()
            return event.angleDelta().x() != 0
        if event.type() == QEvent.Type.KeyPress:
            event = cast(QKeyEvent, event)
            if event.key() == Qt.Key.Key_Escape:
                if self._zoomed_in:
                    self.toggle_zoom()
                else:
                    self._close_selector()
            elif (event.key() == Qt.Key.Key_Enter or event.key() == Qt.Key.Key_Return) and self._zoomed_in:
                self._select_option_and_close(self._zoom_index)
            else:
                try:
                    num_value = int(event.text())
                    if 0 <= num_value < len(self._options):
                        self._zoom_to_option(num_value, True)
                        return True
                    return False
                except ValueError:
                    return False
            return True
        if event.type() == QEvent.Type.MouseButtonPress:
            event = cast(QMouseEvent, event)
            if source == self._view:
                view_pos = event.pos()
            else:
                view_pos = QPoint(self._view.x() + event.pos().x(), self._view.y() + event.pos().y())
            self._view.set_cursor_pos(view_pos)
            if KeyConfig.modifier_held(KeyConfig.PAN_VIEW_MODIFIER):
                return False  # Ctrl+click is for panning, don't select options
            if (event.button() not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton)
                    or AppStateTracker.app_state() == APP_STATE_LOADING):
                return False
            scene_pos = self._view.mapToScene(view_pos).toPoint()
            for i, option in enumerate(self._options):
                if option.bounds.contains(scene_pos):
                    if event.button() == Qt.MouseButton.LeftButton:
                        self._select_option_and_close(i)
                    elif event.button() == Qt.MouseButton.RightButton and source == self._view:
                        self._show_context_menu(i, event.pos())
                    event.accept()
        if event.type() in (QEvent.Type.Enter, QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease):
            event = cast(QSinglePointEvent, event)
            if source == self._view:
                view_pos = event.position().toPoint()
            else:
                view_pos = event.position().toPoint() + self._view.geometry().topLeft()
            self._view.set_cursor_pos(view_pos)
            return False
        if event.type() == QEvent.Type.Leave:
            self._view.set_cursor_pos(None)
        return False

    def _offset_change_slot(self, offset: QPointF) -> None:
        if not self._zoomed_in:
            return
        self._option_pos_offset = QPointF(offset) - QPointF(self._base_option_offset)

    def _scale_change_slot(self, scale: float) -> None:
        if not self._zoomed_in:
            return
        self._option_scale_offset = scale - self._base_option_scale

    def _insert_option_into_layer(self, option_index: int, layer: Layer):
        """Apply an AI-generated image change to the edited image."""
        sample_image = self._options[option_index].image
        if isinstance(sample_image, Image.Image):
            image = pil_image_to_qimage(sample_image).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        else:
            image = sample_image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        if Cache().get(Cache.EDIT_MODE) == EDIT_MODE_INPAINT:
            inpaint_mask = self._image_stack.selection_layer.mask_image
            painter = QPainter(image)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.drawImage(QRect(QPoint(0, 0), image.size()), inpaint_mask)
            painter.end()
        warning_message: Optional[str] = None
        if not isinstance(layer, ImageLayer):
            warning_message = WARNING_MESSAGE_1_NOT_IMAGE
        elif layer.locked or layer.parent_locked:
            warning_message = WARNING_MESSAGE_1_LAYER_LOCKED
        elif not layer.visible:
            warning_message = WARNING_MESSAGE_1_LAYER_HIDDEN
        if warning_message is not None:
            warning_title = WARNING_MESSAGE_1_LAYER_HIDDEN.format(layer_name=layer.name)
            new_layer_name = ORIGINAL_CONTENT_LABEL if option_index == 0 \
                else LABEL_TEXT_IMAGE_OPTION.format(index=option_index)
            warning_message += WARNING_MESSAGE_2_CREATED_NEW.format(new_layer_name=new_layer_name)
            layer = self._image_stack.create_layer(layer_name=new_layer_name)
            assert warning_message is not None
            show_warning_dialog(None, warning_title, warning_message, AppConfig.WARN_WHEN_LOCK_FORCES_LAYER_CREATE)
        self._image_stack.generation_area = self._generation_area
        self._image_stack.set_generation_area_content(image, layer)

    def _select_option_and_close(self, option_index: int) -> None:
        """Insert the selection and close this selector."""
        if option_index != 0:
            layer = self._image_stack.active_layer
            self._insert_option_into_layer(option_index, layer)
        AppStateTracker.set_app_state(APP_STATE_EDITING)
        self._close_selector()

    def _scroll_debounce_finished(self) -> bool:
        ms_time = time.time() * 1000
        if ms_time > self._last_scroll_time + SCROLL_DEBOUNCE_MS:
            self._last_scroll_time = ms_time
            return True
        return False

    def _zoom_to_option(self, option_index: Optional[int] = None, ignore_debounce: bool = False) -> None:
        assert_valid_index(option_index, self._options)
        if not ignore_debounce and not self._scroll_debounce_finished():
            return
        if not self._zoomed_in:
            self._zoomed_in = True
        if option_index is not None:
            self._zoom_index = option_index
        self._view.scale_changed.disconnect(self._scale_change_slot)
        self._view.offset_changed.disconnect(self._offset_change_slot)
        if self._zoom_to_changes and self._change_bounds is not None:
            bounds = QRect(self._change_bounds)
            offset = self._options[self._zoom_index].bounds.topLeft()
            bounds.translate(offset.x(), offset.y())
        else:
            bounds = self._options[self._zoom_index].bounds
        self._view.zoom_to_bounds(bounds)
        self._base_option_offset = self._view.offset
        self._base_option_scale = self._view.scene_scale
        self._view.set_cursor_pos(None)   # Disable cursor tracking when manually adjusting option zoom/offset
        self._view.scene_scale = self._view.scene_scale + self._option_scale_offset
        self._view.offset = self._base_option_offset + self._option_pos_offset
        self._view.scale_changed.connect(self._scale_change_slot)
        self._view.offset_changed.connect(self._offset_change_slot)
        for i, option in enumerate(self._options):
            option.setOpacity(1.0 if not self._zoomed_in or i == self._zoom_index else 0.5)
        if option_index is not None:
            self._page_top_label.setText(self._options[option_index].text)
        self._status_label.setText(self._get_control_hint())

    def _zoom_prev(self):
        idx = len(self._options) - 1 if self._zoom_index <= 0 else self._zoom_index - 1
        self._zoom_to_option(idx)

    def _zoom_next(self):
        idx = 0 if self._zoom_index >= len(self._options) - 1 else self._zoom_index + 1
        self._zoom_to_option(idx)

    def _apply_ideal_image_arrangement(self) -> None:
        """Arrange options in a grid within the scene, choosing grid dimensions to maximize use of available space."""
        if len(self._options) == 0:
            return
        view_width = self._view.size().width()
        view_height = self._view.size().height()
        # All options should have matching sizes:
        image_size = self._options[0].size
        option_count = len(self._options)
        image_margin = int(min(image_size.width(), image_size.height()) * IMAGE_MARGIN_FRACTION)

        def get_scale_factor_for_row_count(row_count: int):
            """Returns the largest image scale multiplier possible to fit images within row_count rows."""
            column_count = math.ceil(option_count / row_count)
            img_bounds = QRect(0, 0, view_width // column_count, view_height // row_count)
            img_rect = get_scaled_placement(img_bounds, image_size, image_margin)
            return img_rect.width() / image_size.width()

        num_rows = 1
        best_scale = 0
        for i in range(1, option_count + 1):
            scale = get_scale_factor_for_row_count(i)
            last_scale = scale
            if scale > best_scale:
                best_scale = scale
                num_rows = i
            elif scale < last_scale:
                break
        num_columns = math.ceil(option_count / num_rows)
        scene_size = QSizeF(num_columns * (image_size.width() + image_margin) - image_margin + VIEW_MARGIN * 2,
                            num_rows * (image_size.height() + image_margin) - image_margin + VIEW_MARGIN * 2)
        view_ratio = self._view.width() / self._view.height()
        scene_ratio = scene_size.width() / scene_size.height()
        scene_x0 = VIEW_MARGIN
        scene_y0 = VIEW_MARGIN
        if scene_ratio < view_ratio:
            new_width = int(view_ratio * scene_size.height())
            scene_x0 += int((new_width - scene_size.width()) / 2)
            scene_size.setWidth(new_width)
        elif scene_ratio > view_ratio:
            new_height = scene_size.width() // view_ratio
            scene_y0 += int((new_height - scene_size.height()) / 2)
            scene_size.setHeight(new_height)

        self._view.content_size = scene_size.toSize()
        for idx in range(option_count):
            row = idx // num_columns
            col = idx % num_columns
            x = scene_x0 + (image_size.width() + image_margin) * col
            y = scene_y0 + (image_size.height() + image_margin) * row
            self._options[idx].setPos(x, y)
            self._outlines[idx].outlined_region = self._options[idx].bounds
            if len(self._selections) > idx:
                selection = self._selections[idx]
                selection.setZValue(self._options[idx].zValue() + 1)
                scale = self._options[idx].bounds.width() / self._image_stack.generation_area.width()
                selection.setScale(scale)
                selection.move_to(QPointF(x / selection.scale(), y / selection.scale()))

    def _get_control_hint(self) -> str:
        config = KeyConfig()
        pan_view_modifier = config.get(KeyConfig.PAN_VIEW_MODIFIER)
        zoom_in_key = config.get(KeyConfig.ZOOM_IN)
        zoom_out_key = config.get(KeyConfig.ZOOM_OUT)
        up_key = config.get(KeyConfig.MOVE_UP)
        down_key = config.get(KeyConfig.MOVE_DOWN)
        if self._zoomed_in:

            return ZOOM_CONTROL_HINT.format(modifier_or_modifiers=get_key_display_string(pan_view_modifier),
                                            left_mouse_icon=left_button_hint_text(),
                                            middle_mouse_icon=middle_button_hint_text(),
                                            v_scroll_icon=vertical_scroll_hint_text(),
                                            zoom_in_hint=get_key_display_string(zoom_in_key),
                                            zoom_out_hint=get_key_display_string(zoom_out_key),
                                            enter_key_hint=get_key_display_string(Qt.Key.Key_Enter),
                                            down_key_hint=get_key_display_string(down_key),
                                            escape_key_hint=get_key_display_string(Qt.Key.Key_Escape))
        return DEFAULT_CONTROL_HINT.format(modifier_or_modifiers=get_key_display_string(pan_view_modifier),
                                           left_mouse_icon=left_button_hint_text(),
                                           middle_mouse_icon=middle_button_hint_text(),
                                           v_scroll_icon=vertical_scroll_hint_text(),
                                           zoom_in_hint=get_key_display_string(zoom_in_key),
                                           zoom_out_hint=get_key_display_string(zoom_out_key),
                                           up_key_hint=get_key_display_string(up_key),
                                           escape_key_hint=get_key_display_string(Qt.Key.Key_Escape))


class _ImageOption(QGraphicsPixmapItem):
    """Displays a generated image option in the view, labeled with a title."""

    def __init__(self, image: QImage, label_text: str) -> None:
        super().__init__()
        self._full_image = image
        self._scaled_image = image
        self._label_text = label_text
        self._transparency_pixmap = get_transparency_tile_pixmap(image.size())
        self.image = image

    @property
    def text(self) -> str:
        """Gets the read-only label text."""
        return self._label_text

    @property
    def image(self) -> QImage:
        """Access the generated image option."""
        return self._scaled_image

    @image.setter
    def image(self, new_image: QImage) -> None:
        cache = Cache()
        config = AppConfig()
        full_size = cache.get(Cache.GENERATION_SIZE)
        final_size = cache.get(Cache.EDIT_SIZE)
        if new_image.size() != full_size:
            self._full_image = new_image
            self._scaled_image = pil_image_scaling(new_image, final_size)
        if new_image.size() == final_size:
            self._full_image = pil_image_scaling(new_image, full_size)
            self._scaled_image = new_image
        else:
            self._full_image = new_image
            self._scaled_image = pil_image_scaling(new_image, final_size)
        self.setPixmap(QPixmap.fromImage(self._full_image if config.get(AppConfig.SHOW_OPTIONS_FULL_RESOLUTION)
                                         else self._scaled_image))
        self.update()

    @property
    def full_image(self) -> QImage:
        """Return the largest available version of this option's image."""
        return self._full_image

    @property
    def bounds(self) -> QRect:
        """Return the image bounds within the scene."""
        return QRect(self.pos().toPoint(), self.size)

    @property
    def size(self) -> QSize:
        """Accesses the image size."""
        return self.pixmap().size()

    @size.setter
    def size(self, new_size) -> None:
        if new_size != self.size:
            self.setPixmap(QPixmap.fromImage(pil_image_scaling(self.image, new_size)))
            self.update()

    @property
    def width(self) -> int:
        """Returns the image width."""
        return self.size.width()

    @property
    def height(self) -> int:
        """Returns the image height."""
        return self.size.height()

    def paint(self,
              painter: Optional[QPainter],
              option: Optional[QStyleOptionGraphicsItem],
              widget: Optional[QWidget] = None) -> None:
        """Draw the label above the image."""
        assert painter is not None
        painter.save()
        image_margin = int(min(self.width, self.height) * IMAGE_MARGIN_FRACTION)
        text_height = image_margin // 2
        text_bounds = QRect(self.width // 4, - text_height - VIEW_MARGIN, self.width // 2, text_height)
        corner_radius = text_bounds.height() // 5
        text_background = QPainterPath()
        text_background.addRoundedRect(QRectF(text_bounds), corner_radius, corner_radius)
        painter.fillPath(text_background, Qt.GlobalColor.black)
        painter.setPen(Qt.GlobalColor.white)
        font = painter.font()
        font_size = int(clamp(font.pointSize(), 1, max_font_size(self._label_text, font, text_bounds.size())))
        font.setPointSize(font_size)
        painter.setFont(font)
        painter.drawText(text_bounds, Qt.AlignmentFlag.AlignCenter, self._label_text)
        if self.opacity() == 1.0:
            painter.drawTiledPixmap(QRect(0, 0, self.width, self.height), self._transparency_pixmap)
        painter.restore()
        super().paint(painter, option, widget)


class _SelectionView(ImageGraphicsView):
    """Minimal ImageGraphicsView controlled by the GeneratedImageSelector"""

    zoom_toggled = Signal()
    content_scrolled = Signal(int, int)

    def __init__(self) -> None:
        super().__init__()
        self.content_size = self.size()

    def scroll_content(self, dx: int | float, dy: int | float) -> bool:
        """Scroll content by the given offset, returning whether content was able to move."""
        self.content_scrolled.emit(int(dx), int(dy))
        return True

    def toggle_zoom(self) -> None:
        """Zoom in on some area of focus, or back to the full scene. Bound to the 'Toggle Zoom' key."""
        self.zoom_toggled.emit()

    def drawBackground(self, painter: Optional[QPainter], rect: QRectF) -> None:
        """Fill with solid black to increase visibility."""
        if painter is not None:
            painter.fillRect(rect, VIEW_BACKGROUND)
        super().drawBackground(painter, rect)
