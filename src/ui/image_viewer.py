"""
Interact with edited image layers through the PyQt5 2D graphics engine.
"""
from typing import Optional

from PyQt5.QtCore import Qt, QRect, QRectF, QSize
from PyQt5.QtGui import QPainter, QColor, QTransform
from PyQt5.QtWidgets import QWidget, QSizePolicy

from src.config.application_config import AppConfig
from src.hotkey_filter import HotkeyFilter
from src.image.layers.image_layer import ImageLayer
from src.image.layers.image_stack import ImageStack
from src.image.layers.layer import Layer
from src.image.layers.layer_stack import LayerStack
from src.ui.graphics_items.border import Border
from src.ui.graphics_items.layer_graphics_item import LayerGraphicsItem
from src.ui.graphics_items.layer_stack_graphics_item import LayerStackGraphicsItem
from src.ui.graphics_items.outline import Outline
from src.ui.graphics_items.polygon_outline import PolygonOutline
from src.ui.widget.image_graphics_view import ImageGraphicsView
from src.util.image_utils import get_transparency_tile_pixmap

GENERATION_AREA_BORDER_OPACITY = 0.6
IMAGE_BORDER_OPACITY = 0.2
GENERATION_AREA_BORDER_COLOR = Qt.GlobalColor.black


class ImageViewer(ImageGraphicsView):
    """Shows the image being edited, and allows the user to select sections."""

    def __init__(self, parent: Optional[QWidget], image_stack: ImageStack) -> None:
        super().__init__(parent)
        HotkeyFilter.instance().set_default_focus(self)
        config = AppConfig()

        self._image_stack = image_stack
        self._generation_area = image_stack.generation_area
        self._layer_stack_item = LayerStackGraphicsItem(self._image_stack.layer_stack)
        self._selection_layer_item = LayerGraphicsItem(self._image_stack.selection_layer)
        scene = self.scene()
        assert scene is not None
        scene.addItem(self._layer_stack_item)
        scene.addItem(self._selection_layer_item)
        self.content_size = image_stack.size
        self.background = get_transparency_tile_pixmap()
        self.setSizePolicy(QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding))
        self._follow_generation_area = False
        self._hidden: set[int] = set()
        self._selection_poly_outline = PolygonOutline(self)
        self._selection_poly_outline.animated = config.get(AppConfig.ANIMATE_OUTLINES)

        # Generation area and border rectangle setup:
        self._image_outline = Outline(scene, self)
        self._image_border = Border(scene, self)
        self._image_border.windowed_area = image_stack.bounds if image_stack.has_image else QRect()
        self._image_border.color = QColor(GENERATION_AREA_BORDER_COLOR)
        self._image_border.setOpacity(IMAGE_BORDER_OPACITY)
        self._image_border.setVisible(True)
        self._image_outline.dash_pattern = [1, 0]  # solid line
        self._generation_area_outline = Outline(scene, self)
        self._generation_area_outline.animated = config.get(AppConfig.ANIMATE_OUTLINES)

        # "inpaint selected only" generation area outline:
        self._image_generation_area_outline = Outline(scene, self)
        self._image_generation_area_outline.setOpacity(GENERATION_AREA_BORDER_OPACITY)
        self._image_generation_area_outline.animated = config.get(AppConfig.ANIMATE_OUTLINES)
        selection_layer = image_stack.selection_layer
        selection_layer.content_changed.connect(self._mask_content_change_slot)
        config.connect(self, AppConfig.INPAINT_FULL_RES, self._mask_content_change_slot)
        config.connect(self, AppConfig.INPAINT_FULL_RES_PADDING, self._mask_content_change_slot)

        # active layer outline:
        self._active_layer_id = -1
        self._active_layer_outline = Outline(scene, self)
        self._active_layer_outline.dash_pattern = [5, 1]  # nearly solid line
        image_stack.active_layer_changed.connect(self._active_layer_change_slot)

        # border drawn when zoomed to image generation area:
        self._generation_area_border = Border(scene, self)
        self._generation_area_border.color = QColor(GENERATION_AREA_BORDER_COLOR)
        self._generation_area_border.setOpacity(GENERATION_AREA_BORDER_OPACITY)
        self._generation_area_border.setVisible(False)

        # Connect image stack event handlers:
        image_stack.content_changed.connect(self._update_drawn_borders)
        image_stack.size_changed.connect(self._image_size_changed_slot)
        image_stack.generation_area_bounds_changed.connect(self._image_generation_area_change_slot)
        image_stack.layer_added.connect(self._layer_added_slot)

        # Manually trigger signal handlers to set up the initial state:
        self._image_size_changed_slot(self.content_size)
        self._layer_added_slot(image_stack.selection_layer)
        for layer in self._image_stack.layers:
            self._layer_added_slot(layer)
        self._image_generation_area_change_slot(image_stack.generation_area)
        self.resizeEvent(None)

    def zoom_to_generation_area(self) -> None:
        """Adjust viewport scale and offset to center the selected editing area in the view."""
        self.zoom_to_bounds(self._image_stack.generation_area)

    def toggle_zoom(self) -> None:
        """Toggles between zooming in on the image generation area and zooming out to the full image view."""
        self.follow_generation_area = not self._follow_generation_area
        if not self.follow_generation_area:
            self.reset_scale()

    def stop_rendering_layer(self, layer: Layer) -> None:
        """Makes the ImageViewer stop direct rendering of a particular layer until further notice."""
        self._hidden.add(layer.id)
        layer_item = self.find_layer_graphics_item(layer.id)
        if layer_item is not None:
            layer_item.hidden = True
            if isinstance(layer, LayerStack):
                for child in layer.recursive_child_layers:
                    self.stop_rendering_layer(child)
        self.update()

    def resume_rendering_layer(self, layer: Layer) -> None:
        """Makes the ImageViewer resume normal rendering for a layer."""
        self._hidden.discard(layer.id)
        layer_item = self.find_layer_graphics_item(layer.id)
        if layer_item is not None:
            layer_item.hidden = False
            if isinstance(layer, LayerStack):
                for child in layer.recursive_child_layers:
                    self.resume_rendering_layer(child)
        self.update()

    def set_layer_opacity(self, layer: ImageLayer, opacity: float) -> None:
        """Updates the rendered opacity of a layer."""
        layer_item = self.find_layer_graphics_item(layer.id)
        if layer_item is None:
            raise KeyError('Layer not yet present in the imageViewer')
        layer_item.setOpacity(opacity)

    @property
    def follow_generation_area(self) -> bool:
        """Returns whether the view is tracking the image generation area."""
        return self._follow_generation_area

    @follow_generation_area.setter
    def follow_generation_area(self, should_follow) -> None:
        """Sets whether the view should follow the image generation area. Setting to true updates the view, setting to
           false does not."""
        self._follow_generation_area = should_follow
        self._generation_area_outline.animated = not should_follow and AppConfig().get(
            AppConfig.ANIMATE_OUTLINES)
        self._generation_area_border.setVisible(should_follow)
        if should_follow:
            self.zoom_to_generation_area()

    def sizeHint(self) -> QSize:
        """Returns image size as ideal widget size."""
        size = self.content_size
        assert size is not None
        return size

    def drawBackground(self, painter: Optional[QPainter], rect: QRectF) -> None:
        """Draw the background as a fixed size tiling image."""
        background = self.background
        assert painter is not None and background is not None
        painter.drawTiledPixmap(rect, background)

    def scroll_content(self, dx: int | float, dy: int | float) -> bool:
        """Scroll the image generation area by the given offset, returning whether it was able to move."""
        generation_area = self._image_stack.generation_area
        self._image_stack.generation_area = generation_area.translated(int(dx), int(dy))
        self.resizeEvent(None)
        return self._image_stack.generation_area != generation_area

    def _update_drawn_borders(self):
        """Make sure that the image generation area and layer borders are in the right place in the scene."""
        generation_area = QRectF(self._generation_area.x(), self._generation_area.y(), self._generation_area.width(),
                                 self._generation_area.height())
        image_loaded = self._image_stack.has_image
        self._image_outline.setVisible(image_loaded)
        self._generation_area_outline.outlined_region = generation_area
        self._generation_area_border.windowed_area = generation_area.toAlignedRect()
        self._generation_area_outline.setVisible(image_loaded)
        self._image_generation_area_outline.setVisible(
            image_loaded and AppConfig().get(AppConfig.INPAINT_FULL_RES))
        if self._image_stack.active_layer is not None:
            self._active_layer_outline.setVisible(True)
            self._active_layer_outline.outlined_region = QRectF(self._image_stack.active_layer.local_bounds)
        else:
            self._active_layer_outline.setVisible(False)
        selection_layer = self._image_stack.selection_layer
        bounds = selection_layer.get_selection_gen_area()
        if bounds is not None:
            self._image_generation_area_outline.setVisible(selection_layer.visible)
            self._image_generation_area_outline.outlined_region = QRectF(bounds)
        else:
            self._image_generation_area_outline.setVisible(False)

    # Signal handlers: sync with image/layer changes:
    # noinspection PyUnusedLocal
    def _active_layer_bounds_changed_slot(self, *args) -> None:
        active_layer = self._image_stack.active_layer
        if active_layer is not None:
            self._active_layer_outline.outlined_region = QRectF(active_layer.local_bounds)

    # noinspection PyUnusedLocal
    def _active_layer_change_slot(self, new_active_layer: Layer, *args) -> None:
        active_id = None if new_active_layer is None else new_active_layer.id
        if active_id != self._active_layer_id:
            last_active = self._image_stack.get_layer_by_id(self._active_layer_id)
            if last_active is not None:
                last_active.transform_changed.disconnect(self._layer_transform_change_slot)
                last_active.size_changed.disconnect(self._active_layer_bounds_changed_slot)
            self._active_layer_id = active_id
            if new_active_layer is not None:
                new_active_layer.transform_changed.connect(self._layer_transform_change_slot)
                new_active_layer.size_changed.connect(self._active_layer_bounds_changed_slot)
                self._active_layer_outline.outlined_region = QRectF(new_active_layer.local_bounds)
                self._active_layer_outline.setTransform(new_active_layer.transform)
                self._active_layer_outline.setVisible(True)
            else:
                self._active_layer_outline.setVisible(False)

    def _mask_content_change_slot(self) -> None:
        """Sync 'inpaint masked only' bounds selection mask layer changes."""
        selection_layer = self._image_stack.selection_layer
        bounds = selection_layer.get_selection_gen_area()
        if bounds is not None:
            self._image_generation_area_outline.setVisible(selection_layer.visible)
            self._image_generation_area_outline.outlined_region = QRectF(bounds)
        else:
            self._image_generation_area_outline.setVisible(False)
        self._selection_poly_outline.setZValue(2)
        self._selection_poly_outline.load_polygons(selection_layer.outline)

    def _image_size_changed_slot(self, new_size: QSize) -> None:
        """Update bounds and background when the image size changes."""
        if new_size.width() <= 0 or new_size.height() <= 0:
            return
        self.content_size = new_size

        self._update_drawn_borders()
        self._image_border.windowed_area = self._image_stack.bounds if self._image_stack.has_image else QRect()
        self._image_outline.outlined_region = self._image_border.windowed_area
        self.resizeEvent(None)

    def _image_generation_area_change_slot(self, new_rect: QRect) -> None:
        """Update the viewer content when the image generation area changes."""
        self._generation_area = new_rect
        self._update_drawn_borders()
        self.resetCachedContent()
        if self.follow_generation_area:
            self.zoom_to_generation_area()
        self.update()

    # noinspection PyUnusedLocal
    def _layer_transform_change_slot(self, layer: Layer, transform: QTransform) -> None:
        """Apply layer transformations to outlines."""
        if layer == self._image_stack.active_layer:
            self._active_layer_outline.setTransform(layer.full_image_transform)

    def _layer_added_slot(self, new_layer: Layer) -> None:
        """Adds a new image layer into the view."""
        if self._image_border.windowed_area.isEmpty() and self._image_stack.has_image:
            self._image_border.windowed_area = self._image_stack.bounds if self._image_stack.has_image else QRect()
            self._image_outline.outlined_region = self._image_border.windowed_area
        layer_item = self.find_layer_graphics_item(new_layer.id)
        assert layer_item is not None
        for outline in (self._generation_area_outline, self._image_generation_area_outline, self._active_layer_outline,
                        self._generation_area_border):
            outline.setZValue(max(self._generation_area_outline.zValue(), new_layer.z_value + 1))
        if new_layer.id in self._hidden:
            layer_item.hidden = True
        if layer_item.isVisible():
            self.resetCachedContent()
            self.update()

    def find_layer_graphics_item(self, layer_id: int) -> Optional[LayerGraphicsItem | LayerStackGraphicsItem]:
        """Returns the graphics item representing a layer, or None if the layer isn't found"""
        if layer_id == self._layer_stack_item.layer.id:
            return self._layer_stack_item
        if layer_id == self._selection_layer_item.layer.id:
            return self._selection_layer_item
        return self._layer_stack_item.find_layer_item(layer_id)

