"""
Basic interface for tools that interact with image data.

Supports the following:
- Consumes key and mouse events with scene coordinates, use them to make arbitrary changes.
- Provide a tool panel widget with UI controls.
- Override the cursor when over the image viewer.
"""
from typing import Optional

from PySide6.QtCore import QObject, Signal, QPoint, QEvent, Qt
from PySide6.QtGui import QCursor, QPixmap, QMouseEvent, QTabletEvent, QWheelEvent, QIcon
from PySide6.QtWidgets import QWidget, QApplication

from src.config.cache import Cache
from src.config.key_config import KeyConfig
from src.image.layers.image_layer import ImageLayer
from src.image.layers.image_stack import ImageStack
from src.image.layers.layer import Layer
from src.ui.modal.modal_utils import show_error_dialog
from src.util.shared_constants import ERROR_MESSAGE_LAYER_NONE, ERROR_MESSAGE_LAYER_LOCKED, \
    ERROR_MESSAGE_LAYER_GROUP_LOCKED, ERROR_MESSAGE_LAYER_HIDDEN, ERROR_MESSAGE_EMPTY_MASK, ERROR_TITLE_EDIT_FAILED
from src.util.visual.image_utils import image_is_fully_transparent
from src.util.visual.text_drawing_utils import left_button_hint_text, middle_button_hint_text, \
    vertical_scroll_hint_text, get_key_display_string

# The `QCoreApplication.translate` context for strings in this file
TR_ID = 'tools.base_tool'


def _tr(*args):
    """Helper to make `QCoreApplication.translate` more concise."""
    return QApplication.translate(TR_ID, *args)


PAN_HINT = _tr('{modifier_or_modifiers}+{left_mouse_icon} or {middle_mouse_icon}, drag: pan view')
ZOOM_HINT = _tr('{v_scroll_icon}: zoom')
FIXED_ASPECT_HINT = _tr('{modifier_or_modifiers}: Fixed aspect ratio')


# noinspection PyMethodMayBeStatic
class BaseTool(QObject):
    """
    Basic interface for tools that interact with image data.

    To extend:
    ----------
    1. Implement get_icon, get_label_text, and get_tooltip_text to provide descriptive information.

    2. If needed, implement on_activate to handle setup tasks and on_deactivate to handle cleanup tasks.

    3.  Implement all needed event handling functions, probably by acting on the ImageStack. All event handling
       functions receive the associated QEvent and the associated set of image coordinates if relevant. Event handlers
       should return True if the event was consumed, False if any default event handling should still take effect.
       Event handlers may be called without a QEvent to trigger their behavior manually.

    4.  If needed, override get_control_panel to return a QWidget with a UI for adjusting tool properties or reporting
       tool information.

    5.  If the tool should change the cursor, use `tool.cursor = cursor` to change the cursor when the tool is active
       and the pointer is over the image.  Use `tool.cursor = None` to go back to using the default cursor with the
       tool.  Tool cursors can be either a QCursor or a QPixmap, but QPixmap should only be used for extra large
       cursors that might not work well with the windowing system.

    To integrate:
    -------------
    1. Prepare an image display component with mouse tracking, make sure it provides a way to convert widget
       coordinates to image coordinates.

    2. Use get_icon, get_label_text, and get_tooltip text to represent the tool in the UI.

    3. When a tool enters active use, do the following:
       - Set tool.is_active = True
       - Display the widget returned by get_control_panel (if not None) somewhere in the UI.
       - Set the image widget cursor to tool cursor, if not None.
       - Connect to the cursor_change signal to handle cursor updates.

    4. When applying cursors, if the cursor is a Pixmap instead of a  QCursor, instead use a minimal cursor and
       manually draw the pixmap over the mouse cursor. If the cursor is None, use the default cursor.

    5. When the image display component receives QEvents, calculate the associated image coordinates if relevant and
       call the associated BaseTool event function. To support, MouseEnter and MouseExit, use the mouse events to flag
       whether the cursor is over the image and keep track of when that flag changes. If the event function returns
       True, the image widget shouldn't do anything else with that event.

    6. When a tool exits active use, set tool.is_active=False and disconnect from the cursor_changed signal.
    """

    cursor_change = Signal()

    def __init__(self, activation_config_key: str, label_text: str, tooltip_text: str, icon: QIcon) -> None:
        super().__init__()
        self._activation_config_key = activation_config_key
        self._cursor: Optional[QCursor | QPixmap] = None
        self._saved_cursor: Optional[QCursor | QPixmap] = None
        self._disabled_cursor = QCursor(Qt.CursorShape.ForbiddenCursor)
        self._active = False
        self._label_text = label_text
        self._tooltip_text = tooltip_text
        self._icon = icon

    @staticmethod
    def modifier_hint(modifier_key: str, modifier_hint_str: str) -> str:
        """Returns a hint string with a config-defined modifier inserted, or the empty string if the modifier is not
           defined."""
        assert '{modifier_or_modifiers}' in modifier_hint_str
        if KeyConfig().get_modifier(modifier_key) == Qt.KeyboardModifier.NoModifier:
            return ''
        modifier = KeyConfig().get(modifier_key)
        modifier = get_key_display_string(modifier)
        return modifier_hint_str.format(modifier_or_modifiers=modifier)

    @staticmethod
    def fixed_aspect_hint() -> str:
        """Returns the hint for the fixed aspect ratio key, if set"""
        return f'{BaseTool.modifier_hint(KeyConfig.FIXED_ASPECT_MODIFIER, FIXED_ASPECT_HINT)}'

    @property
    def cursor(self) -> Optional[QCursor | QPixmap]:
        """Returns the active tool cursor or tool pixmap."""
        return self._cursor

    @cursor.setter
    def cursor(self, new_cursor: Optional[QCursor | QPixmap]) -> None:
        """Sets the active tool cursor or tool pixmap."""
        if self._cursor != self._disabled_cursor:
            self._cursor = new_cursor
            if self.is_active:
                self.cursor_change.emit()
        else:
            self._saved_cursor = new_cursor

    def set_disabled_cursor(self, use_disabled_cursor: bool) -> None:
        """Sets or removes a cursor indicating that input is disabled."""
        if use_disabled_cursor == (self._cursor == self._disabled_cursor):
            return
        if use_disabled_cursor:
            self._saved_cursor = self._cursor
            self._cursor = self._disabled_cursor
        else:
            self._cursor = self._saved_cursor
            self._saved_cursor = None
        if self.is_active:
            self.cursor_change.emit()

    @property
    def is_active(self) -> bool:
        """Returns whether this tool is currently marked as active."""
        return self._active

    @is_active.setter
    def is_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        if active:
            self._on_activate()
        else:
            self._on_deactivate()

    def reactivate_after_delegation(self) -> None:
        """Sets the tool as active again after temporarily disabling it to delegate inputs to another tool."""
        assert not self._active
        self._active = True
        self._on_activate(True)

    def get_activation_config_key(self) -> str:
        """Returns the KeyConfig value key for the hotkey that activates this tool."""
        return self._activation_config_key

    def get_icon(self) -> QIcon:
        """Returns an icon used to represent this tool."""
        return self._icon

    def get_label_text(self) -> str:
        """Returns label text used to represent this tool."""
        return self._label_text

    def get_tooltip_text(self) -> str:
        """Returns tooltip text used to describe this tool."""
        return self._tooltip_text

    def get_input_hint(self) -> str:
        """Return text describing different input functionality."""
        pan_hint = PAN_HINT.format(left_mouse_icon=left_button_hint_text(),
                                   middle_mouse_icon=middle_button_hint_text(),
                                   modifier_or_modifiers='{modifier_or_modifiers}')
        zoom_hint = ZOOM_HINT.format(v_scroll_icon=vertical_scroll_hint_text())
        return f'{BaseTool.modifier_hint(KeyConfig.PAN_VIEW_MODIFIER, pan_hint)} - {zoom_hint}'

    @property
    def label(self) -> str:
        """Also expose the tool label as the 'label' property."""
        return self.get_label_text()

    def get_control_panel(self) -> Optional[QWidget]:
        """Returns a panel providing controls for customizing tool behavior, or None if no such panel is needed."""
        return None

    def validate_layer(self, layer: Optional[Layer], require_image_layer=True, show_error_messages=True,
                       image_stack: Optional[ImageStack] = None) -> bool:
        """Check if a layer can accept changes."""
        error_message: Optional[str] = None
        if layer is None or (require_image_layer and not isinstance(layer, ImageLayer)):
            error_message = ERROR_MESSAGE_LAYER_NONE
        elif layer.locked:
            error_message = ERROR_MESSAGE_LAYER_LOCKED
        elif layer.parent_locked:
            error_message = ERROR_MESSAGE_LAYER_GROUP_LOCKED
        elif not layer.visible:
            error_message = ERROR_MESSAGE_LAYER_HIDDEN
        elif image_stack is not None and Cache().get(Cache.PAINT_SELECTION_ONLY):
            mask_image = image_stack.selection_layer.image_bits_readonly
            if image_is_fully_transparent(mask_image):
                error_message = ERROR_MESSAGE_EMPTY_MASK
        if show_error_messages and error_message is not None:
            show_error_dialog(None, ERROR_TITLE_EDIT_FAILED, error_message)
        return error_message is None

    def _on_activate(self, restoring_after_delegation=False) -> None:
        """Called when the tool becomes active, implement to handle any setup that needs to be done."""

    def _on_deactivate(self) -> None:
        """Called when the tool stops being active, implement to handle any cleanup that needs to be done."""

    # Event handlers:

    def mouse_click(self, event: Optional[QMouseEvent], image_coordinates: QPoint) -> bool:
        """Receives a mouse click event, returning whether the tool consumed the event."""
        return False

    # noinspection PyUnusedLocal
    def mouse_double_click(self, event: Optional[QMouseEvent], image_coordinates: QPoint) -> bool:
        """Receives a mouse double click event, returning whether the tool consumed the event."""
        return False

    def mouse_move(self, event: Optional[QMouseEvent], image_coordinates: QPoint) -> bool:
        """Receives a mouse move event, returning whether the tool consumed the event."""
        return False

    def mouse_release(self, event: Optional[QMouseEvent], image_coordinates: QPoint) -> bool:
        """Receives a mouse release event, returning whether the tool consumed the event."""
        return False

    # noinspection PyUnusedLocal
    def mouse_enter(self, event: Optional[QEvent], image_coordinates: QPoint) -> bool:
        """Receives a mouse enter event, returning whether the tool consumed the event.

        Mouse enter events are non-standard, the widget managing this tool needs to identify these itself by tracking
        mouse event coordinates and detecting when the cursor moves inside the image bounds.
        """
        return False

    # noinspection PyUnusedLocal
    def mouse_exit(self, event: Optional[QEvent], image_coordinates: QPoint) -> bool:
        """Receives a mouse exit event, returning whether the tool consumed the event.

        Mouse exit events are non-standard, the widget managing this tool needs to identify these itself by tracking
        mouse event coordinates and detecting when the cursor moves outside the image bounds.
        """
        return False

    def tablet_event(self, event: Optional[QTabletEvent], image_coordinates: QPoint) -> bool:
        """Receives a graphics tablet input event, returning whether the tool consumed the event."""
        return False

    def wheel_event(self, event: Optional[QWheelEvent]) -> bool:
        """Receives a mouse wheel scroll event, returning whether the tool consumed the event."""
        return False
