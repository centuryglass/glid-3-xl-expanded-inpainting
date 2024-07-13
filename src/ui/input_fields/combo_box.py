"""A simple wrapper for QComboBox to give it an interface consistent with other input widgets."""
from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QComboBox, QWidget


class ComboBox(QComboBox):
    """A simple wrapper for QComboBox to give it an interface consistent with other input widgets."""

    valueChanged = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._data_type = str
        self.currentIndexChanged.connect(self._index_change_slot)

    # noinspection PyPep8Naming
    def addItem(self, text: Optional[str], userData: Any = None) -> None:
        """Ensure options always have data attached."""
        if userData is None:
            userData = text
        if not isinstance(userData, self._data_type):
            raise TypeError(f'Item {text} should have data type {self._data_type}, found {userData}')
        super().addItem(text, userData)

    def _index_change_slot(self, index: int) -> None:
        data = self.itemData(index)
        if not isinstance(data, self._data_type):
            raise TypeError(f'Item {index} should have data type {self._data_type}, found {data}')
        self.valueChanged.emit(self._data_type(data))

    def value(self) -> Any:
        """Returns the current selected item value."""
        data = self.currentData()
        if not isinstance(data, self._data_type):
            raise TypeError(f'Item should have data type {self._data_type}, found {data}')
        return data

    def setValue(self, new_value: Any) -> None:
        """Updates the current selected item value."""
        index = self.findData(new_value)
        if index < 0:
            raise ValueError(f'{new_value} not present in ComboBox')
        self.setCurrentIndex(index)
