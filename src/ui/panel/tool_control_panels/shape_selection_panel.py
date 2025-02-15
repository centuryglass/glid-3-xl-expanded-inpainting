"""Selection panel for the ShapeSelectionTool class."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout

from src.config.key_config import KeyConfig
from src.hotkey_filter import HotkeyFilter
from src.image.layers.selection_layer import SelectionLayer
from src.tools.base_tool import BaseTool
from src.ui.input_fields.dual_toggle import DualToggle
from src.ui.layout.divider import Divider
from src.ui.panel.tool_control_panels.selection_panel import SelectionPanel
from src.ui.widget.key_hint_label import KeyHintLabel
from src.util.shared_constants import PROJECT_DIR
from src.util.visual.shape_mode import SHAPE_MODE_RECTANGLE_LABEL, SHAPE_MODE_ELLIPSE_LABEL

ICON_PATH_RECT = f'{PROJECT_DIR}/resources/icons/tool_modes/rect_select.svg'
ICON_PATH_ELLIPSE = f'{PROJECT_DIR}/resources/icons/tool_modes/ellipse_select.svg'


class ShapeSelectionPanel(SelectionPanel):
    """Selection panel for the SelectionFillTool class."""

    tool_mode_changed = Signal(str)

    def __init__(self, selection_layer: SelectionLayer, selection_tool: BaseTool) -> None:
        super().__init__(selection_layer, selection_tool)

        toggle_row = QHBoxLayout()
        toggle_row.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._mode_toggle = DualToggle(self, [SHAPE_MODE_RECTANGLE_LABEL, SHAPE_MODE_ELLIPSE_LABEL],
                                       Qt.Orientation.Horizontal)
        self._mode_toggle.set_icons(ICON_PATH_RECT, ICON_PATH_ELLIPSE)
        self._mode_toggle.setValue(SHAPE_MODE_RECTANGLE_LABEL)
        self._mode_toggle.valueChanged.connect(self.tool_mode_changed)
        toggle_row.addWidget(self._mode_toggle)

        toggle_hint = KeyHintLabel(config_key=KeyConfig.TOOL_ACTION_HOTKEY, parent=self)
        toggle_row.addWidget(toggle_hint)
        self.insert_into_layout(toggle_row)

        def _try_toggle() -> bool:
            if not self.selection_tool_is_active:
                return False
            self._mode_toggle.toggle()
            return True
        binding_id = f'ShapeSelectionPanel_{id(self)}_try_toggle'
        HotkeyFilter.instance().register_config_keybinding(binding_id, _try_toggle, KeyConfig.TOOL_ACTION_HOTKEY)

        # TODO: fixed aspect ratio checkbox
        self.insert_into_layout(Divider(Qt.Orientation.Horizontal))
