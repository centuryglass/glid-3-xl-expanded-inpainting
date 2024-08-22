"""Base template for tools that use a MyPaint surface within an image layer."""
import logging
from typing import Optional

from PySide6.QtCore import Qt, QPoint, QSize, QRect, QLineF, QPointF
from PySide6.QtGui import QCursor, QTabletEvent, QMouseEvent, QColor, QIcon, QWheelEvent, QPointingDevice
from PySide6.QtWidgets import QApplication

from src.config.application_config import AppConfig
from src.config.cache import Cache
from src.config.key_config import KeyConfig
from src.hotkey_filter import HotkeyFilter
from src.image.canvas.layer_canvas import LayerCanvas
from src.image.layers.image_layer import ImageLayer
from src.image.layers.image_stack import ImageStack
from src.image.layers.layer import Layer
from src.tools.base_tool import BaseTool
from src.ui.image_viewer import ImageViewer
from src.util.shared_constants import PROJECT_DIR, FLOAT_MAX

RESOURCES_BRUSH_ICON = f'{PROJECT_DIR}/resources/icons/tools/brush_icon.svg'
RESOURCES_CURSOR = f'{PROJECT_DIR}/resources/cursors/brush_cursor.svg'
RESOURCES_MIN_CURSOR = f'{PROJECT_DIR}/resources/cursors/min_cursor.svg'


# The `QCoreApplication.translate` context for strings in this file
TR_ID = 'tools.canvas_tool'


def _tr(*args):
    """Helper to make `QCoreApplication.translate` more concise."""
    return QApplication.translate(TR_ID, *args)


LINE_HINT = _tr('{modifier_or_modifiers}+click: line mode - ')
FIXED_ANGLE_HINT = _tr('{modifier_or_modifiers}: fixed angle - ')

MAX_CURSOR_SIZE = 255
MIN_CURSOR_SIZE = 20
MIN_SMALL_CURSOR_SIZE = 15
MIN_LINE_PRESSURE = 0.5

logger = logging.getLogger(__name__)


class CanvasTool(BaseTool):
    """Base template for tools that use a LayerCanvas to edit an image layer using drawing commands.

    CanvasTool handles the connection between a layer and a MyPaint canvas, and the process of applying inputs to
    that canvas. Implementations are responsible for providing their own control panel, configuring brush properties,
    and setting or updating the affected layer.
    """

    def __init__(self, image_stack: ImageStack, image_viewer: ImageViewer, canvas: LayerCanvas) -> None:
        super().__init__()
        self._layer: Optional[ImageLayer] = None
        self._drawing = False
        self._cached_size: Optional[int] = None
        self._tablet_pressure: Optional[float] = None
        self._tablet_x_tilt: Optional[float] = None
        self._tablet_y_tilt: Optional[float] = None
        self._tablet_input: Optional[QPointingDevice.PointerType] = None
        self._last_pos: Optional[QPoint] = None
        self._fixed_angle: Optional[int] = None

        self._small_brush_icon = QIcon(RESOURCES_MIN_CURSOR)
        self._small_brush_cursor = QCursor(self._small_brush_icon.pixmap(MIN_SMALL_CURSOR_SIZE, MIN_SMALL_CURSOR_SIZE))
        self._default_scaled_cursor_icon = QIcon(RESOURCES_CURSOR)
        self._scaled_icon_cursor: Optional[QIcon] = self._default_scaled_cursor_icon
        self._scaling_cursor = True
        image_viewer.scale_changed.connect(self.update_brush_cursor)

        # Create MyPaintLayerCanvas
        self._image_stack = image_stack
        self._image_viewer = image_viewer
        self._canvas = canvas

        def update_size(new_size: QSize) -> None:
            """Sync canvas size with image size."""
            self._canvas.edit_region = QRect(QPoint(0, 0), new_size)
        self._image_stack.size_changed.connect(update_size)

        for key, sign in ((KeyConfig.BRUSH_SIZE_DECREASE, -1), (KeyConfig.BRUSH_SIZE_INCREASE, 1)):
            def _size_change(mult, step=sign) -> bool:
                if not self.is_active:
                    return False
                self.adjust_brush_size(step * mult)
                return True

            HotkeyFilter.instance().register_speed_modified_keybinding(_size_change, key)

        # Handle restricting changes to selection:
        def _set_restricted_to_selection(selected_only: bool) -> None:
            if not self.is_active or self._layer is None or self._layer == image_stack.selection_layer:
                return
            if selected_only:
                mask = image_stack.get_layer_mask(self._layer)
                self._canvas.set_input_mask(mask)
            else:
                self._canvas.set_input_mask(None)
        Cache().connect(self, Cache.PAINT_SELECTION_ONLY, _set_restricted_to_selection)

        def _selection_layer_update(_) -> None:
            if not Cache().get(Cache.PAINT_SELECTION_ONLY) or not self.is_active or self._layer is None \
                    or self._layer == image_stack.selection_layer:
                return
            mask = image_stack.get_layer_mask(self._layer)
            self._canvas.set_input_mask(mask)
        image_stack.selection_layer.content_changed.connect(_selection_layer_update)

    @staticmethod
    def canvas_control_hints() -> str:
        """Get control hints for line and fixed angle modes, if enabled."""
        return (f'{BaseTool.modifier_hint(KeyConfig.LINE_MODIFIER, LINE_HINT)}'
                f'{BaseTool.modifier_hint(KeyConfig.FIXED_ASPECT_MODIFIER, FIXED_ANGLE_HINT)}')

    def set_scaling_icon_cursor(self, icon: Optional[QIcon]) -> None:
        """Sets whether the tool should use a cursor scaled to the brush size and canvas.

        Parameters
        ----------
        icon: QIcon, optional
            Cursor icon to use. If None, dynamic cursor updates will be disabled. If an icon, CanvasTool will
            dynamically scale it to match the brush size, using an alternate instead if it's below MIN_CURSOR_SIZE.
        """
        self._scaling_cursor = icon is not None
        self._scaled_icon_cursor = icon
        self.update_brush_cursor()

    def reset_scaling_pixmap_cursor(self) -> None:
        """Restores the default scaling brush cursor."""
        if self._scaling_cursor and self._scaled_icon_cursor == self._default_scaled_cursor_icon:
            return
        self._scaling_cursor = True
        self._scaled_icon_cursor = self._default_scaled_cursor_icon
        self.update_brush_cursor()

    @property
    def layer(self) -> Optional[ImageLayer]:
        """Returns the active image layer."""
        return self._layer

    @layer.setter
    def layer(self, layer: Optional[ImageLayer]) -> None:
        """Sets or clears the connected image layer."""
        if self._layer is not None and self._layer != layer:
            self._layer.lock_changed.disconnect(self._layer_lock_slot)
            if isinstance(self._layer, ImageLayer):
                self._image_viewer.resume_rendering_layer(self._layer)
        self._layer = layer
        if layer is not None:
            layer.lock_changed.connect(self._layer_lock_slot)
            self._layer_lock_slot(layer, layer.locked)
            if layer != self._image_stack.selection_layer and Cache().get(Cache.PAINT_SELECTION_ONLY):
                mask = self._image_stack.get_layer_mask(layer)
                self._canvas.set_input_mask(mask)
        if not self._active:
            return
        if layer is None or not isinstance(layer, ImageLayer):
            self._canvas.connect_to_layer(None)
            self._image_viewer.hide_active_layer = False
            control_panel = self.get_control_panel()
            if control_panel is not None:
                control_panel.setEnabled(False)

    def _layer_lock_slot(self, layer: Layer, locked: bool) -> None:
        if not self.is_active:
            return
        assert layer == self._layer
        if locked:
            self._canvas.connect_to_layer(None)
            self._image_viewer.hide_active_layer = False
        else:
            self._canvas.z_value = layer.z_value
            self._canvas.connect_to_layer(layer)
            self._image_viewer.stop_rendering_layer(layer)
        control_panel = self.get_control_panel()
        if control_panel is not None:
            control_panel.setEnabled(not locked)

    @property
    def brush_size(self) -> int:
        """Gets the active brush size."""
        return self._canvas.brush_size

    @brush_size.setter
    def brush_size(self, new_size: int):
        """Updates the active brush size."""
        self.set_brush_size(new_size)

    @property
    def brush_path(self) -> Optional[str]:
        """Gets the active brush file path, if any."""
        if hasattr(self._canvas, 'brush_path'):
            return self._canvas.brush_path
        return None

    @brush_path.setter
    def brush_path(self, new_path: str) -> None:
        """Updates the active brush size."""
        if hasattr(self._canvas, 'brush_path'):
            try:
                self._canvas.brush_path = new_path
            except OSError as err:
                logger.error(f'loading brush {new_path} failed', err)
        else:
            raise RuntimeError(f'Tried to set brush path {new_path} when layer canvas has no brush support.')

    def adjust_brush_size(self, offset: int) -> None:
        """Change brush size by some offset amount, multiplying offset if the speed modifier is held."""
        if KeyConfig.modifier_held(KeyConfig.SPEED_MODIFIER):
            offset *= AppConfig().get(AppConfig.SPEED_MODIFIER_MULTIPLIER)
        self.set_brush_size(max(self._canvas.brush_size + offset, 1))

    def set_brush_size(self, new_size: int) -> None:
        """Update the brush size."""
        self._canvas.brush_size = max(new_size, 1)
        self.update_brush_cursor()

    @property
    def brush_color(self) -> QColor:
        """Gets the active brush color."""
        return self._canvas.brush_color

    @brush_color.setter
    def brush_color(self, new_color: QColor | Qt.GlobalColor) -> None:
        """Updates the active brush color."""
        self._canvas.brush_color = QColor(new_color)

    def _on_activate(self, restoring_after_delegation=False) -> None:
        """Connect the canvas to the active layer."""
        if self._layer is not None:
            self.layer = self._layer  # Re-apply the connection
        self.update_brush_cursor()

    def _on_deactivate(self) -> None:
        """Disconnect from the image when the tool is inactive."""
        if self._drawing:
            self._drawing = False
            if self._cached_size:
                self.brush_size = self._cached_size
                self._cached_size = None
            self._canvas.end_stroke()
            self._tablet_input = None
            self._tablet_pressure = None
            self._tablet_x_tilt = None
            self._tablet_y_tilt = None
        if self._layer is not None:
            self._image_viewer.resume_rendering_layer(self._layer)
        self._canvas.connect_to_layer(None)

    # Event handlers:
    def _stroke_to(self, image_coordinates: QPoint) -> None:
        """Draws coordinates to the canvas, including tablet data if available."""
        if self._layer is not None:
            image_coordinates = self._layer.map_from_image(image_coordinates)
        if not self._image_stack.has_image:
            return
        if self._tablet_input == QPointingDevice.PointerType.Eraser:
            self._canvas.eraser = True

        if KeyConfig.modifier_held(KeyConfig.FIXED_ANGLE_MODIFIER) and self._last_pos is not None \
                and self._last_pos != image_coordinates:
            closest_point = None
            last_pos = QPointF(self._last_pos)
            if self._fixed_angle is None:
                distance_line = QLineF(last_pos, QPointF(image_coordinates))
                min_distance = FLOAT_MAX
                for angle in range(0, 360, 45):
                    distance_line.setAngle(angle)
                    distance_from_mouse = QLineF(QPointF(image_coordinates), distance_line.p2()).length()
                    if distance_from_mouse < min_distance:
                        self._fixed_angle = angle
                        min_distance = distance_from_mouse
                        closest_point = distance_line.p2().toPoint()
            else:
                line = QLineF(last_pos, last_pos + QPointF(1.0, 0.0))
                line.setAngle(self._fixed_angle)
                normal_line = QLineF(QPointF(image_coordinates), QPointF(image_coordinates) + QPointF(1.0, 0.0))
                normal_line.setAngle(self._fixed_angle + 90)
                intersect_type, closest_point = line.intersects(normal_line)
                assert intersect_type != QLineF.IntersectionType.NoIntersection, f'{line} parallel to {normal_line}'
                assert isinstance(closest_point, QPointF)
                closest_point = closest_point.toPoint()
            assert isinstance(closest_point, QPoint)
            image_coordinates = closest_point
        if KeyConfig.modifier_held(KeyConfig.LINE_MODIFIER) and self._last_pos is not None:
            pressure = None if self._tablet_pressure is None else max(self._tablet_pressure, MIN_LINE_PRESSURE)
            self._canvas.stroke_to(self._last_pos.x(), self._last_pos.y(), pressure, self._tablet_x_tilt,
                                   self._tablet_y_tilt)
            self._canvas.stroke_to(image_coordinates.x(), image_coordinates.y(), pressure,
                                   self._tablet_x_tilt, self._tablet_y_tilt)
        else:
            self._canvas.stroke_to(image_coordinates.x(), image_coordinates.y(), self._tablet_pressure,
                                   self._tablet_x_tilt, self._tablet_y_tilt)

        if self._tablet_input == QPointingDevice.PointerType.Eraser:
            self._canvas.eraser = False
        self._last_pos = image_coordinates

    def mouse_click(self, event: Optional[QMouseEvent], image_coordinates: QPoint) -> bool:
        """Starts drawing when the mouse is clicked in the scene."""
        if self._layer is None or event is None or not self._image_stack.has_image:
            return False
        if KeyConfig.modifier_held(KeyConfig.PAN_VIEW_MODIFIER, True):
            return False
        if event.buttons() == Qt.MouseButton.LeftButton or event.buttons() == Qt.MouseButton.RightButton:
            if self._drawing:
                self._canvas.end_stroke()
            self._drawing = True
            self._fixed_angle = None
            if KeyConfig.modifier_held(KeyConfig.FIXED_ANGLE_MODIFIER):
                self._last_pos = image_coordinates  # Update so previous clicks don't constrain the angle
            if self._cached_size is not None and event.buttons() == Qt.MouseButton.LeftButton:
                self.brush_size = self._cached_size
                self._cached_size = None
            elif event.buttons() == Qt.MouseButton.RightButton:
                if self._cached_size is None:
                    self._cached_size = self._canvas.brush_size
                self.brush_size = 1
            self._canvas.start_stroke()
            self._stroke_to(image_coordinates)
            return True
        return False

    def mouse_move(self, event: Optional[QMouseEvent], image_coordinates: QPoint) -> bool:
        """Receives a mouse move event, returning whether the tool consumed the event."""
        if self._layer is None or event is None or not self._image_stack.has_image:
            return False
        if (event.buttons() == Qt.MouseButton.LeftButton or event.buttons() == Qt.MouseButton.RightButton
                and self._drawing):
            self._stroke_to(image_coordinates)
            return True
        return False

    def mouse_release(self, event: Optional[QMouseEvent], image_coordinates: QPoint) -> bool:
        """Receives a mouse release event, returning whether the tool consumed the event."""
        if self._layer is None or event is None or not self._image_stack.has_image:
            return False
        if self._drawing:
            self._drawing = False
            self._stroke_to(image_coordinates)
            self._canvas.end_stroke()
            if self._cached_size:
                self.brush_size = self._cached_size
                self._cached_size = None
            self._tablet_input = None
            self._tablet_pressure = None
            self._tablet_x_tilt = None
            self._tablet_y_tilt = None
            return True
        return False

    def tablet_event(self, event: Optional[QTabletEvent], image_coordinates: QPoint) -> bool:
        """Cache tablet data when received."""
        assert event is not None
        if event.pointerType() is not None:
            self._tablet_input = event.pointerType()
        if event.pressure() > 0.0001:
            self._tablet_pressure = event.pressure()
        else:
            self._tablet_pressure = None
        self._tablet_x_tilt = event.xTilt()
        self._tablet_y_tilt = event.yTilt()
        return True

    def update_brush_cursor(self) -> None:
        """Recalculates the brush cursor size if using a scaling cursor."""
        if not self.is_active or self._scaling_cursor is False or self._scaled_icon_cursor is None:
            return
        brush_cursor_size = int(self.brush_size * self._image_viewer.scene_scale)
        if brush_cursor_size <= MIN_SMALL_CURSOR_SIZE:
            self.cursor = self._small_brush_cursor
        else:
            icon = self._scaled_icon_cursor if brush_cursor_size > MIN_CURSOR_SIZE else self._small_brush_icon
            scaled_cursor = icon.pixmap(brush_cursor_size, brush_cursor_size)
            if brush_cursor_size > MAX_CURSOR_SIZE:
                self.cursor = scaled_cursor
            else:
                self.cursor = QCursor(scaled_cursor)

    def wheel_event(self, event: Optional[QWheelEvent]) -> bool:
        """Adjust brush size if scrolling horizontal."""
        assert event is not None
        if not hasattr(self, 'adjust_brush_size'):
            return False
        offset = 0
        if event.angleDelta().x() < 0:
            offset -= 1
        elif event.angleDelta().x() > 0:
            offset += 1
        if offset != 0:
            if KeyConfig.modifier_held(KeyConfig.SPEED_MODIFIER):
                offset *= AppConfig().get(AppConfig.SPEED_MODIFIER_MULTIPLIER)
            self.adjust_brush_size(offset)
            return True
        return False
