"""
A shared resource to set inpainting configuration and to provide default values.

Main features
-------------
    - Save and load values from a JSON file.
    - Allow access to those values through config.get()
    - Allow typesafe changes to those values through config.set()
    - Subscribe to specific value changes through config.connect()
"""
import json
import logging
import os.path
import threading
from inspect import signature
from threading import Lock
from typing import Optional, Any, Callable

from PySide6.QtCore import QSize, QTimer, Qt
from PySide6.QtGui import QKeySequence, QColor
from PySide6.QtWidgets import QApplication

from src.config.config_entry import ConfigEntry, DefinitionKey, DefinitionType
from src.ui.input_fields.check_box import CheckBox
from src.ui.input_fields.combo_box import ComboBox
from src.util.parameter import ParamType, DynamicFieldWidget, ParamTypeList, get_parameter_type
from src.util.signals_blocked import signals_blocked

logger = logging.getLogger(__name__)

# The `QCoreApplication.translate` context for strings in this file
TR_ID = 'config.config'


def _tr(*args):
    """Helper to make `QCoreApplication.translate` more concise."""
    return QApplication.translate(TR_ID, *args)


MISSING_DEF_ERROR = _tr('Config definition file not found at {definition_path}')
INVALID_CONFIG_TYPE_ERROR = _tr('Config value definition for {key} had invalid data type {value_type}')
INVALID_KEY_ERROR = _tr('Loading {key} failed: {err}')
INVALID_JSON_DEFINITION_ERROR = _tr('Reading JSON config definitions failed: {err}')
INVALID_JSON_ERROR = _tr('Reading JSON config values failed: {err}')
UNKNOWN_KEY_ERROR = _tr('Tried to access unknown config value "{key}"')
INVALID_OPTION_KEY_ERROR = _tr('Tried to track fixed options for key "{key}", which does not have a fixed list of'
                               ' options.')
INVALID_KEYCODE_ERROR = _tr('Tried to get key code "{key}", found "{code_string}"')
DUPLICATE_KEY_ERROR = _tr('Tried to add duplicate config entry for key "{key}"')


class Config:
    """A shared resource to set inpainting configuration and to provide default values.

    Common Exceptions Raised
    ------------------------
    KeyError
        When any function with the `key` parameter is called with an unknown key.
    TypeError
        When a function with the optional `inner_key` parameter is used with a non-empty `inner_key` and a `key`
        that doesn't contain a dict value.
    RuntimeError
        If a function that interacts with lists of accepted value options is called on a value that doesn't have
        a fixed list of acceptable options.
    """

    def __init__(self, definition_path: str, saved_value_path: Optional[str], child_class: type) -> None:
        """Load existing config, or initialize from defaults.

        Parameters
        ----------
        definition_path: str
            Path to a file defining accepted config values.
        saved_value_path: str, optional
            Path where config values will be saved and read. If the file does not exist, it will be created with
            default values. Any expected keys not found in the file will be added with default values. Any unexpected
            values will be removed. If not provided, the Config object won't allow file IO.
        child_class: class
            Child class where definition keys should be written as properties when first initialized.
        """
        self._entries: dict[str, ConfigEntry] = {}
        self._connected: dict[str, dict[Any, Callable[..., None]]] = {}
        self._option_connected: dict[str, dict[Any, Callable[[ParamTypeList], None]]] = {}
        self._json_path = saved_value_path
        self._lock = Lock()
        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)

        if not os.path.isfile(definition_path):
            raise RuntimeError(MISSING_DEF_ERROR.format(definition_path=definition_path))
        try:
            with open(definition_path, encoding='utf-8') as file:
                config_text_key = definition_path.replace('_definitions.json', '')

                def _tr_cfg(text: str) -> str:
                    return QApplication.translate(config_text_key, text)

                json_data = json.load(file)
                for key, definition in json_data.items():
                    assert isinstance(definition, dict)
                    init_attr_name = key.upper()
                    if not hasattr(child_class, init_attr_name):
                        setattr(child_class, init_attr_name, key)
                    try:
                        if DefinitionKey.DEFAULT in definition:
                            initial_value = definition[DefinitionKey.DEFAULT]
                            match definition[DefinitionKey.TYPE]:
                                case DefinitionType.QSIZE:
                                    initial_value = QSize(*(int(n) for n in initial_value.split('x')))
                                case DefinitionType.INT:
                                    initial_value = int(initial_value)
                                case DefinitionType.FLOAT:
                                    initial_value = float(initial_value)
                                case DefinitionType.STR:
                                    initial_value = str(initial_value)
                                case DefinitionType.BOOL:
                                    initial_value = bool(initial_value)
                                case DefinitionType.LIST:
                                    initial_value = list(initial_value)
                                case DefinitionType.DICT:
                                    initial_value = dict(initial_value)
                                case _:
                                    raise RuntimeError(INVALID_CONFIG_TYPE_ERROR.format(key=key,
                                                                                        value_type=definition[
                                                                                            DefinitionKey.TYPE]))
                        else:  # If no default is provided, use the closest equivalent to an empty value:
                            match definition[DefinitionKey.TYPE]:
                                case DefinitionType.QSIZE:
                                    initial_value = QSize()
                                case DefinitionType.INT:
                                    initial_value = 0
                                case DefinitionType.FLOAT:
                                    initial_value = 0.0
                                case DefinitionType.STR:
                                    initial_value = ''
                                case DefinitionType.BOOL:
                                    initial_value = False
                                case DefinitionType.LIST:
                                    initial_value = []
                                case DefinitionType.DICT:
                                    initial_value = {}
                                case _:
                                    raise RuntimeError(INVALID_CONFIG_TYPE_ERROR.format(key=key,
                                                                                        value_type=definition[
                                                                                            DefinitionKey.TYPE]))
                    except KeyError as err:
                        raise RuntimeError(INVALID_KEY_ERROR.format(key=key, err=err)) from err

                    label = _tr_cfg(definition[DefinitionKey.LABEL])
                    category = _tr_cfg(definition[DefinitionKey.CATEGORY])
                    subcategory = None if DefinitionKey.SUBCATEGORY not in definition \
                        else _tr_cfg(definition[DefinitionKey.SUBCATEGORY])
                    tooltip = _tr_cfg(definition[DefinitionKey.TOOLTIP])
                    options = None if DefinitionKey.OPTIONS not in definition \
                        else list(definition[DefinitionKey.OPTIONS])
                    range_options = None if DefinitionKey.RANGE not in definition \
                        else dict(definition[DefinitionKey.RANGE])
                    if DefinitionKey.SAVED in definition:
                        save_json = definition[DefinitionKey.SAVED]
                    else:
                        save_json = False
                    self._add_entry(key, initial_value, label, category, subcategory, tooltip, options, range_options,
                                    save_json)

        except json.JSONDecodeError as err:
            raise RuntimeError(INVALID_JSON_DEFINITION_ERROR.format(err=err)) from err

        self._adjust_defaults()
        if self._json_path is not None:
            if os.path.isfile(self._json_path):
                self._read_from_json()
            else:
                self._write_to_json()

    # noinspection PyProtectedMember
    def _reset(self) -> None:
        """Discard all changes and connections, and reload from JSON. For testing use only."""
        with self._lock:
            self._connected = {}
            for key, entry in self._entries.items():
                self._connected[key] = {}
                entry._value = entry.default_value
                if entry._options is not None and len(entry._options) > 0 and entry.default_value is not None:
                    entry._options = [entry.default_value]

    def _adjust_defaults(self) -> None:
        """Override this to perform any adjustments to default values needed before file IO, e.g. loading list options
           from an external source."""

    @property
    def json_path(self) -> Optional[str]:
        """Returns the path where this config object saves changes. If None, the config object does not save data to
         disk."""
        return self._json_path

    def get(self, key: str, inner_key: Optional[str] = None) -> Any:
        """Returns a value from config.

        Parameters
        ----------
        key : str
            A key tracked by this config file.
        inner_key : str, optional
            If not None, assume the value at `key` is a dict and attempt to return the value within it at `inner_key`.
            If the value is a dict but does not contain `inner_key`, instead return None

        Returns
        -------
        int or float or str or bool or list or dict or QSize or None
            Type varies based on key. Each key is guaranteed to always return the same type, but inner_key values
            are not type-checked.
        """
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        with self._lock:
            return self._entries[key].get_value(inner_key)

    def get_data_type(self, key: str) -> str:
        """Gets the data type associated with a config key, raising KeyError if the key isn't found."""
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        return self._entries[key].type_name

    def get_category(self, key: str) -> str:
        """Returns a config value's category, raising KeyError if the value does not exist."""
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        return self._entries[key].category

    def get_subcategory(self, key: str) -> Optional[str]:
        """Returns a config value's subcategory, raising KeyError if the value does not exist."""
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        return self._entries[key].subcategory

    def get_color(self, key: str, default_color: QColor | Qt.GlobalColor) -> QColor:
        """Returns a color value from config.

        Parameters
        ----------
        key : str
            A key tracked by this config file.
        default_color : QColor | Qt.GlobalColor
            Color value to return if the key doesn't map to a valid color string.

        Returns
        -------
        The configured color, or the default if the color isn't valid."""
        color_str = self.get(key)
        if not QColor.isValidColor(color_str):
            if isinstance(default_color, Qt.GlobalColor):
                default_color = QColor(default_color)
            return default_color
        return QColor(color_str)

    def get_control_widget(self, key: str, connect_to_config: bool = True, multi_line=False) -> DynamicFieldWidget:
        """Returns a QWidget capable of adjusting the chosen config value. Unless connect_to_config is false, changes
        will immediately propagate to the underlying config file."""
        with self._lock:
            entry = self._entries[key]
            control_widget = entry.get_input_widget(multi_line, False)
            control_widget.setValue(entry.get_value())
            if isinstance(control_widget, CheckBox):
                control_widget.setText(entry.name)
            if connect_to_config:
                config_key = key

                def _update_config(new_value: Any) -> None:
                    self.set(config_key, new_value)

                assert hasattr(control_widget, 'valueChanged')
                control_widget.valueChanged.connect(_update_config)

                def _update_control(new_value: Any) -> None:
                    if control_widget.value() != new_value:
                        if isinstance(control_widget, ComboBox):
                            current_options = self.get_options(config_key)
                            widget_options = [control_widget.itemText(i) for i in range(control_widget.count())]
                            if widget_options != current_options:
                                with signals_blocked(control_widget):
                                    while control_widget.count() > 0:
                                        control_widget.removeItem(0)
                                    for new_option in current_options:
                                        control_widget.addItem(str(new_option), userData=new_option)
                        control_widget.setValue(new_value)

                self.connect(control_widget, key, _update_control)

                if isinstance(control_widget, ComboBox):
                    combobox = control_widget

                    def _update_options(new_options: ParamTypeList) -> None:
                        last_selected_text = combobox.currentText()
                        with signals_blocked(combobox):
                            while combobox.count() > 0:
                                combobox.removeItem(0)
                            for option in new_options:
                                combobox.addItem(str(option), userData=option)
                            new_index = combobox.findText(last_selected_text)
                            if new_index >= 0:
                                combobox.setCurrentIndex(new_index)

                    self.connect_to_option_changes(combobox, key, _update_options)
            return control_widget

    def get_keycodes(self, key: str) -> QKeySequence:
        """Returns a config value as a key sequence, throws RuntimeError if the value isn't a keycode."""
        code_string = self.get(key)
        if not isinstance(code_string, str):
            raise RuntimeError(INVALID_KEYCODE_ERROR.format(key=key, code_string=code_string))
        sequence = QKeySequence(code_string)
        if Qt.Key.Key_unknown in sequence:
            raise RuntimeError(INVALID_KEYCODE_ERROR.format(key=key, code_string=code_string))
        return sequence

    def get_label(self, key: str) -> str:
        """Gets the label text assigned to a config value."""
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        return _tr(self._entries[key].name)

    def get_tooltip(self, key: str) -> str:
        """Gets the tooltip text assigned to a config value."""
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        return _tr(self._entries[key].description)

    def set(self,
            key: str,
            value: Any,
            save_change: bool = True,
            add_missing_options: bool = False,
            inner_key: Optional[str] = None) -> None:
        """Updates a saved value.

        Parameters
        ----------
        key : str
            A key tracked by this config file.
        value : int or float or str or bool or list or dict or QSize or None
            The new value to assign to the key. Unless inner_key is not None, this must have the same type as the
            previous value.
        save_change: bool, default=True
            If true, save the change to the underlying JSON file. Otherwise, the change will be saved the next time
            any value is set with save_change=True
        add_missing_options: bool, default=False
            If the key is associated with a list of valid options and this is true, value will be added to the list
            of options if not already present. Otherwise, RuntimeError is raised if value is not within the list
        inner_key: str, optional
            If not None, assume the value at `key` is a dict and attempt to set the value within it at `inner_key`. If
            the value is a dict but does not contain `inner_key`, instead return None
       
        Raises
        ------
        TypeError
            If `value` does not have the same type as the current value saved under `key`
        RuntimeError
            If `key` has a list of associated valid options, `value` is not one of those options, and
            `add_missing_options` is false.
        """
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        new_value = value
        # Update existing value:
        with self._lock:
            value_changed = self._entries[key].set_value(value, add_missing_options, inner_key)
        if not value_changed:
            return
        # Schedule save to JSON file:
        if save_change:
            with self._lock:
                if not self._save_timer.isActive():
                    if threading.current_thread() is not threading.main_thread():
                        self._write_to_json()  # Timers can't be started from other threads.
                    else:
                        def write_change() -> None:
                            """Copy changes to the file and disconnect the timer."""
                            self._write_to_json()
                            self._save_timer.timeout.disconnect(write_change)

                        self._save_timer.timeout.connect(write_change)
                        self._save_timer.start(10)
        # Pass change to connected callback functions:
        callbacks = [*self._connected[key].items()]  # <- So callbacks can disconnect or replace themselves
        for source, callback in callbacks:
            num_args = len(signature(callback).parameters)
            try:
                if num_args == 0 and inner_key is None:
                    callback()
                elif num_args == 1 and inner_key is None:
                    callback(new_value)
                elif num_args == 2:
                    callback(new_value, inner_key)
            except RuntimeError as err:
                if 'already deleted' in str(err):
                    logger.warning(f'Disconnecting from {key}, got error={err}')
                    self.disconnect(source, key)
                else:
                    raise err
            if self.get(key, inner_key) != value:
                break

    def connect(self,
                connected_object: Any,
                key: str,
                on_change_fn: Callable[..., None],
                inner_key: Optional[str] = None) -> None:
        """
        Registers a callback function that should run when a particular key is changed.

        Parameters
        ----------
        connected_object: object
            An object to associate with this connection. Only one connection can be made between a given key and
            connected_object.
        key: str
            A key tracked by this config file.
        on_change_fn: function(new_value), function(new_value, inner_key)
            The function to run when the value changes.
        inner_key: str, optional
            If not None, assume the value at `key` is a dict and ensure on_change_fn only runs when `inner_key`
            changes within the value.
        """
        if key not in self._connected:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        num_args = len(signature(on_change_fn).parameters)
        if num_args > 2:
            raise RuntimeError(f'callback function connected to {key} value takes {num_args} '
                               'parameters, expected 0-2')
        if inner_key is None:
            self._connected[key][connected_object] = on_change_fn
        else:
            def wrapper_fn(value: Any, changed_inner_key: str) -> None:
                """Call connected function only if the inner key changes."""
                if changed_inner_key == inner_key:
                    on_change_fn(value)

            self._connected[key][connected_object] = wrapper_fn

    def connect_to_option_changes(self,
                                  connected_object: Any,
                                  key: str,
                                  on_change_fn: Callable[[ParamTypeList], None]) -> None:
        """
        Registers a callback function that should run when fixed options change for a particular key.

        Parameters
        ----------
        connected_object: object
            An object to associate with this connection. Only one options connection can be made between a given key and
            connected_object.
        key: str
            A key tracked by this config file.
        on_change_fn: function(new_options), function(new_value, inner_key)
            The function to run when the option list changes.
        """
        if key not in self._option_connected:
            raise KeyError(INVALID_OPTION_KEY_ERROR.format(key=key))
        self._option_connected[key][connected_object] = on_change_fn

    def disconnect(self, connected_object: Any, key: str) -> None:
        """
        Removes a callback function previously registered through config.connect() or config.connect_to_option_changes
        for a particular object and key.
        """
        if key not in self._connected:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        self._connected[key].pop(connected_object, None)
        if key in self._option_connected:
            self._option_connected[key].pop(connected_object, None)

    def disconnect_all(self, connected_object: Any) -> None:
        """Removes all connections associated with a particular object."""
        for connection_list in self._connected.values():
            connection_list.pop(connected_object, None)
        for option_connection_list in self._option_connected.values():
            option_connection_list.pop(connected_object, None)

    def get_option_index(self, key: str) -> int:
        """Returns the index of the selected option for a given key.

        Raises
        ------
        RuntimeError
            If the value associated with the key does not have a predefined list of options
        """
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        with self._lock:
            return self._entries[key].option_index

    def get_options(self, key: str) -> ParamTypeList:
        """Returns all valid options accepted for a given key.

        Raises
        ------
        RuntimeError
            If the value associated with the key does not have a predefined list of options.
        """
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        options = self._entries[key].options
        if options is None:
            raise RuntimeError(f'{key} has no options list.')
        return options

    def get_default_options(self, key: str) -> ParamTypeList:
        """Returns the default set of valid options accepted for a given key.

        Raises
        ------
        RuntimeError
            If the value associated with the key does not have a predefined list of options.
        """
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        options = self._entries[key].default_options()
        if options is None:
            raise RuntimeError(f'{key} has no default options list.')
        return options

    def update_options(self, key: str, options_list: ParamTypeList) -> None:
        """
        Replaces the list of accepted options for a given key.

        Raises
        ------
        RuntimeError
            If the value associated with the key does not have a predefined list of options.
        """
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        last_value = self.get(key)
        self._entries[key].set_valid_options(options_list)

        callbacks = [*self._option_connected[key].items()]  # <- So callbacks can disconnect or replace themselves
        for source, callback in callbacks:
            try:
                callback([*options_list])
            except RuntimeError as err:
                if 'already deleted' in str(err):
                    logger.warning(f'Disconnecting from {key}, got error={err}')
                    self.disconnect(source, key)
                else:
                    raise err
        if last_value not in options_list and len(options_list) > 0:
            self.set(key, options_list[0])

    def restore_default_options(self, key: str) -> None:
        """
        Restores the default options list for a given key.

        Raises
        ------
        RuntimeError
            If the value associated with the key does not have a predefined list of options.
        """
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        default_options = self._entries[key].default_options()
        if default_options is not None:
            self.update_options(key, default_options)

    def add_option(self, key: str, option: ParamType) -> None:
        """
        Adds a new item to the list of accepted options for a given key.

        Raises
        ------
        RuntimeError
            If the value associated with the key does not have a predefined list of options.
        """
        if key not in self._entries:
            raise KeyError(UNKNOWN_KEY_ERROR.format(key=key))
        option_param_type = get_parameter_type(option)
        if option_param_type != self._entries[key].type_name:
            raise TypeError(f'Key "{key}": expected type "{self._entries[key].type_name}", but new option "{option}" '
                            f'has type {option_param_type}')
        all_options = self._entries[key].options
        assert all_options is not None
        if option not in all_options:
            all_options.append(option)  # type: ignore
            self.update_options(key, all_options)

    def get_categories(self) -> list[str]:
        """Returns all unique category strings."""
        categories = []
        for value in self._entries.values():
            if value.category not in categories:
                categories.append(value.category)
        return categories

    def get_subcategories(self, category: str) -> list[str]:
        """Returns all unique subcategories within a category."""
        subcategories = []
        for value in self._entries.values():
            if value.category != category:
                continue
            if value.subcategory is not None and value.subcategory not in subcategories:
                subcategories.append(value.subcategory)
        return subcategories

    def get_category_keys(self, category: str, subcategory: Optional[str] = None) -> list[str]:
        """Returns all keys with the given category."""
        keys = []
        for key, value in self._entries.items():
            if value.category == category and (subcategory is None or subcategory == value.subcategory):
                keys.append(key)
        return keys

    def get_keys(self) -> list[str]:
        """Returns all keys defined for a config class."""
        return list(self._entries.keys())

    def _add_entry(self,
                   key: str,
                   initial_value: Any,
                   label: str,
                   category: str,
                   subcategory: Optional[str],
                   tooltip: str,
                   options: Optional[list[ParamType]] = None,
                   range_options: Optional[dict[str, int | float]] = None,
                   save_json: bool = True) -> None:
        if key in self._entries:
            raise KeyError(DUPLICATE_KEY_ERROR.format(key=key))
        entry = ConfigEntry(key, initial_value, label, category, subcategory, tooltip, options, range_options,
                            save_json)
        self._entries[key] = entry
        self._connected[key] = {}
        if options is not None:
            self._option_connected[key] = {}

    def _write_to_json(self) -> None:
        if self._json_path is None:
            return
        converted_dict: dict[str, Any] = {}
        with self._lock:
            for entry in self._entries.values():
                entry.save_to_json_dict(converted_dict)
            with open(self._json_path, 'w', encoding='utf-8') as file:
                json.dump(converted_dict, file, ensure_ascii=False, indent=4)

    def _read_from_json(self) -> None:
        if self._json_path is None:
            return
        json_data = None
        try:
            with open(self._json_path, encoding='utf-8') as file:
                json_data = json.load(file)
        except json.JSONDecodeError as err:
            logger.error(INVALID_JSON_ERROR.format(err=err))
        if json_data is None:  # Invalid JSON, replace it with defaults
            self._write_to_json()
            return
        with self._lock:
            for entry in self._entries.values():
                entry.load_from_json_dict(json_data)
