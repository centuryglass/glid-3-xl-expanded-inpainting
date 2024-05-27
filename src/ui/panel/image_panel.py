"""Panel used to display the edited image and associated controls. """
from typing import Optional
from PyQt5.QtWidgets import QWidget, QSpinBox, QDoubleSpinBox, QLabel, QVBoxLayout, QHBoxLayout, QSlider, QSizePolicy, \
    QPushButton
from PyQt5.QtCore import Qt, QRect, QSize, pyqtSignal
from PyQt5.QtGui import QPainter, QPen

from src.ui.image_viewer import ImageViewer
from src.ui.config_control_setup import connected_textedit
from src.ui.util.contrast_color import contrast_color
from src.ui.widget.param_slider import ParamSlider
from src.ui.widget.collapsible_box import CollapsibleBox
from src.config.application_config import AppConfig
from src.image.layer_stack import LayerStack
from src.util.validation import assert_type

IMAGE_PANEL_TITLE = 'Full Image'
SCALE_SLIDER_LABEL = 'Zoom:'
SCALE_RESET_BUTTON_LABEL = 'Reset View'
SCALE_RESET_BUTTON_TOOLTIP = 'Zoom out to view the entire image.'
SCALE_ZOOM_BUTTON_LABEL = 'Zoom to selection'
SCALE_ZOOM_BUTTON_TOOLTIP = 'Zoom in on the area selected for image generation'
SCALE_RESET_BUTTON_TOOLTIP = 'Restore default image zoom and offset'
SELECTION_X_LABEL = 'X:'
SELECTION_X_TOOLTIP = 'Selected X coordinate'
SELECTION_Y_LABEL = 'Y:'
SELECTION_Y_TOOLTIP = 'Selected Y coordinate'
SELECTION_WIDTH_LABEL = 'W:'
SELECTION_HEIGHT_LABEL = 'H:'


class ImagePanel(QWidget):
    """Holds the image viewer, provides inputs for selecting an editing area and saving/loading images."""

    image_toggled = pyqtSignal(bool)

    def __init__(self, config: AppConfig, layer_stack: LayerStack):
        """Initializes the panel layout.

        Parameters
        ----------
        config : AppConfig
            Shared application configuration object.
        layer_stack : LayerStack
            Image layers being edited.
        """
        super().__init__()

        layer_stack.size_changed.connect(lambda new_size: self.reload_scale_bounds())
        self._layer_stack = layer_stack
        self._config = config
        self._show_sliders = None
        self._slider_count = 0
        self._minimized = False
        self._border_size = 4
        self._image_box_layout = None

        self._layout = QHBoxLayout()
        self.setLayout(self._layout)

        self._image_box = CollapsibleBox(IMAGE_PANEL_TITLE,
                                         parent=self,
                                         scrolling=False,
                                         orientation=Qt.Orientation.Horizontal)
        self._image_box.toggled().connect(self.image_toggled.emit)
        self._image_box.set_expanded_size_policy(QSizePolicy.Ignored)

        self._image_viewer = ImageViewer(self, layer_stack)

        self._control_bar = QWidget()
        control_bar_layout = QHBoxLayout(self._control_bar)
        control_bar_layout.addWidget(QLabel(config.get_label(AppConfig.LAST_FILE_PATH)))
        self._file_text_box = connected_textedit(self, config, AppConfig.LAST_FILE_PATH)
        control_bar_layout.addWidget(self._file_text_box, stretch=100)

        # image zoom controls
        self._image_scale_slider = QSlider(Qt.Orientation.Horizontal)
        self._image_scale_slider.setRange(1, 4000)
        self._image_scale_slider.setSingleStep(10)
        self._image_scale_slider.setValue(int(self._image_viewer.scale * 100))
        self._image_scale_box = QDoubleSpinBox()
        self._image_scale_box.setRange(0.001, 40)
        self._image_scale_box.setSingleStep(0.1)
        self._image_scale_box.setValue(self._image_viewer.scale)

        scale_signals = [
            self._image_viewer.scale_changed,
            self._image_scale_slider.valueChanged,
            self._image_scale_box.valueChanged
        ]

        control_bar_layout.addWidget(QLabel(SCALE_SLIDER_LABEL))
        control_bar_layout.addWidget(self._image_scale_slider, stretch=100)
        control_bar_layout.addWidget(self._image_scale_box)

        def toggle_scale():
            """Toggle between default zoom and zooming in on the editing selection."""
            if self._image_viewer.is_at_default_view and not self._image_viewer.follow_selection:
                self._image_viewer.follow_selection = True
                self.scale_reset_button.setText(SCALE_RESET_BUTTON_LABEL)
                self.scale_reset_button.setToolTip(SCALE_RESET_BUTTON_TOOLTIP)
            else:
                self._image_viewer.reset_scale()
                self.scale_reset_button.setText(SCALE_ZOOM_BUTTON_LABEL)
                self.scale_reset_button.setToolTip(SCALE_ZOOM_BUTTON_TOOLTIP)

        self.scale_reset_button = QPushButton()
        self.scale_reset_button.setText(SCALE_ZOOM_BUTTON_LABEL)
        self.scale_reset_button.setToolTip(SCALE_ZOOM_BUTTON_TOOLTIP)

        self.scale_reset_button.clicked.connect(toggle_scale)
        control_bar_layout.addWidget(self.scale_reset_button)
        def on_scale_change(new_scale: float | int) -> None:
            """Synchronize slider, spin box, panel scale, and zoom button text:"""
            if isinstance(new_scale, int):
                float_scale = new_scale / 100
                int_scale = new_scale
            else:
                float_scale = new_scale
                int_scale = int(float_scale * 100)
            for scale_signal in scale_signals:
                scale_signal.disconnect(on_scale_change)
            if self._image_scale_box.value() != float_scale:
                self._image_scale_box.setValue(float_scale)
            if self._image_scale_slider.value() != int_scale:
                self._image_scale_slider.setValue(int_scale)
            if self._image_viewer.scale != float_scale:
                self._image_viewer.scale = float_scale
            for scale_signal in scale_signals:
                scale_signal.connect(on_scale_change)
            if self._image_viewer.is_at_default_view and not self._image_viewer.follow_selection:
                self.scale_reset_button.setText(SCALE_ZOOM_BUTTON_LABEL)
                self.scale_reset_button.setToolTip(SCALE_ZOOM_BUTTON_TOOLTIP)
            else:
                self.scale_reset_button.setText(SCALE_RESET_BUTTON_LABEL)
                self.scale_reset_button.setToolTip(SCALE_RESET_BUTTON_TOOLTIP)

        for signal in scale_signals:
            signal.connect(on_scale_change)


        # wire x/y coordinate boxes to set selection coordinates:
        control_bar_layout.addWidget(QLabel(SELECTION_X_LABEL, self))
        self._x_coord_box = QSpinBox(self)
        control_bar_layout.addWidget(self._x_coord_box)
        self._x_coord_box.setRange(0, 0)
        self._x_coord_box.setToolTip(SELECTION_X_TOOLTIP)

        def set_x(value: int):
            """Handle selection x-coordinate changes."""
            last_selected = layer_stack.selection
            last_selected.moveLeft(min(value, layer_stack.width - last_selected.width()))
            layer_stack.selection = last_selected

        self._x_coord_box.valueChanged.connect(set_x)

        control_bar_layout.addWidget(QLabel(SELECTION_Y_LABEL, self))
        self._y_coord_box = QSpinBox(self)
        control_bar_layout.addWidget(self._y_coord_box)
        self._y_coord_box.setRange(0, 0)
        self._y_coord_box.setToolTip(SELECTION_Y_TOOLTIP)

        def set_y(value: int):
            """Handle selection y-coordinate changes."""
            last_selected = layer_stack.selection
            last_selected.moveTop(min(value, layer_stack.height - last_selected.height()))
            layer_stack.selection = last_selected

        self._y_coord_box.valueChanged.connect(set_y)

        # Selection size controls:
        control_bar_layout.addWidget(QLabel(SELECTION_WIDTH_LABEL, self))
        self._width_box = QSpinBox(self)
        control_bar_layout.addWidget(self._width_box)

        control_bar_layout.addWidget(QLabel(SELECTION_HEIGHT_LABEL, self))
        self._height_box = QSpinBox(self)
        control_bar_layout.addWidget(self._height_box)

        edit_size = config.get(AppConfig.EDIT_SIZE)
        min_edit_size = config.get(AppConfig.MIN_EDIT_SIZE)
        max_edit_size = config.get(AppConfig.MAX_EDIT_SIZE)
        for size_control, type_name, min_size, max_size, size in [
                (self._width_box, 'width', min_edit_size.width(), max_edit_size.width(), edit_size.width()),
                (self._height_box, 'height', min_edit_size.height(), max_edit_size.height(), edit_size.height())]:
            size_control.setToolTip(f'Selected area {type_name}')
            size_control.setRange(min_size, max_size)
            size_control.setSingleStep(min_size)
            size_control.setValue(size)

        def set_w():
            """Handle selection width changes."""
            value = self._width_box.value()
            selection = layer_stack.selection
            selection.setWidth(value)
            layer_stack.selection = selection
            edit_size = config.get(AppConfig.EDIT_SIZE)
            config.set(AppConfig.EDIT_SIZE, QSize(layer_stack.selection.width(), edit_size.height()))

        self._width_box.editingFinished.connect(set_w)

        def set_h():
            """Handle selection height changes."""
            value = self._height_box.value()
            selection = layer_stack.selection
            selection.setHeight(value)
            layer_stack.selection = selection
            edit_size = config.get(AppConfig.EDIT_SIZE)
            config.set(AppConfig.EDIT_SIZE, QSize(edit_size.width(), layer_stack.selection.height()))

        self._height_box.editingFinished.connect(set_h)

        def set_coordinates(bounds: QRect):
            """Update coordinate controls automatically when the selection changes."""
            self._x_coord_box.setValue(bounds.left())
            self._y_coord_box.setValue(bounds.top())
            self._width_box.setValue(bounds.width())
            self._height_box.setValue(bounds.height())
            self._width_box.setMaximum(min(max_edit_size.width(), layer_stack.width))
            self._height_box.setMaximum(min(max_edit_size.height(), layer_stack.height))
            self._x_coord_box.setRange(0, layer_stack.width - bounds.width())
            self._y_coord_box.setRange(0, layer_stack.height - bounds.height())

        layer_stack.selection_bounds_changed.connect(set_coordinates)
        set_coordinates(layer_stack.selection)
        self.setLayout(self._layout)
        self.setSizePolicy(QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding))
        self._init_image_box_layout()
        self.show_sliders(False)

    def set_orientation(self, orientation: Qt.Orientation):
        """Sets the panel to a vertical or horizontal Qt.Orientation."""
        prev_image_box = self._image_box
        if self._image_box is not None:
            self._layout.removeWidget(self._image_box)
        self._image_box = CollapsibleBox('Full Image',
                                         parent=self,
                                         scrolling=False,
                                         orientation=orientation)
        self._init_image_box_layout()
        self._image_box.toggled().connect(self.image_toggled.emit)
        self._layout.insertWidget(self._slider_count, self._image_box)
        if prev_image_box is not None:
            prev_image_box.setParent(None)

    def add_slider(self, slider: QSlider | ParamSlider):
        """Adds a QSlider or ParamSlider control widget to the panel."""
        assert_type(slider, (ParamSlider, QSlider))
        self._layout.insertWidget(self._slider_count, slider, stretch=1)
        self._slider_count += 1
        self.show_sliders(self._show_sliders)

    def sliders_showing(self):
        """Returns whether sliders added with add_slider are visible."""
        return self._show_sliders

    def show_sliders(self, show_sliders: bool):
        """Shows or hides all sliders added with add_slider."""
        self._show_sliders = show_sliders
        if show_sliders:
            for i in range(self._slider_count):
                self._layout.setStretch(i, 1)
            for slider in (self._layout.itemAt(i).widget() for i in range(self._slider_count)):
                slider.setVisible(True)
                slider.setEnabled(True)
                slider.setMaximumWidth(slider.sizeHint().width())
        else:
            for i in range(self._slider_count):
                self._layout.setStretch(i, 0)
            for slider in (self._layout.itemAt(i).widget() for i in range(self._slider_count)):
                slider.setEnabled(False)
                slider.setVisible(False)
                slider.setMaximumWidth(0)
        self._image_box.show_button_bar(True)

    def reload_scale_bounds(self):
        """Recalculate image scaling bounds based on image size and edit size limits."""
        max_edit_size = self._layer_stack.max_selection_size
        image_size = self._layer_stack.size
        for spinbox, max_edit_dim in [(self._width_box, max_edit_size.width()),
                                      (self._height_box, max_edit_size.height())]:
            spinbox.setMaximum(max_edit_dim)
        selection_size = self._layer_stack.selection.size()
        self._x_coord_box.setMaximum(image_size.width() - selection_size.width())
        self._y_coord_box.setMaximum(image_size.height() - selection_size.height())

    def paintEvent(self, unused_event):
        """Draws a border around the panel."""
        painter = QPainter(self)
        painter.setPen(QPen(contrast_color(self), self._border_size / 2, Qt.SolidLine,
                            Qt.RoundCap, Qt.RoundJoin))
        painter.drawRect(1, 1, self.width() - 2, self.height() - 2)

    def _init_image_box_layout(self):
        self._image_box_layout = QVBoxLayout()
        self._image_box_layout.addWidget(self._image_viewer, stretch=255)
        self._image_box_layout.addWidget(self._control_bar)
        self._image_box.set_content_layout(self._image_box_layout)
        self._layout.addWidget(self._image_box, stretch=255)
        self._image_box.set_content_layout(self._image_box_layout)

