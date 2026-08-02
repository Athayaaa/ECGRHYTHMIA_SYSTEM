"""Load PyQt6 when available and fall back to Ubuntu Jammy's PyQt5 package."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

try:
    from PyQt6 import QtCore, QtGui, QtWidgets

    QT_API = "PyQt6"
except ImportError:  # Ubuntu 22.04 ships PyQt5, but not the PyQt6 bindings.
    from PyQt5 import QtCore as _QtCore
    from PyQt5 import QtGui as _QtGui
    from PyQt5 import QtWidgets as _QtWidgets

    QT_API = "PyQt5"

    class _TypeProxy:
        """Keep Qt6 scoped enum spelling usable with Qt5's legacy constants."""

        def __init__(self, target: Any, **attributes: Any) -> None:
            self._target = target
            self.__dict__.update(attributes)

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            return self._target(*args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._target, name)

    class _ModuleProxy:
        def __init__(self, target: Any, **attributes: Any) -> None:
            self._target = target
            self.__dict__.update(attributes)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._target, name)

    qt = _QtCore.Qt
    qt_proxy = _TypeProxy(
        qt,
        MouseButton=SimpleNamespace(NoButton=qt.NoButton),
        TimerType=SimpleNamespace(PreciseTimer=qt.PreciseTimer),
        AlignmentFlag=SimpleNamespace(
            AlignCenter=qt.AlignCenter,
            AlignHCenter=qt.AlignHCenter,
            AlignTop=qt.AlignTop,
        ),
        Key=SimpleNamespace(
            Key_Home=qt.Key_Home,
            Key_Left=qt.Key_Left,
            Key_Right=qt.Key_Right,
            Key_End=qt.Key_End,
            Key_Space=qt.Key_Space,
            Key_R=qt.Key_R,
        ),
        Orientation=SimpleNamespace(Horizontal=qt.Horizontal),
        TextInteractionFlag=SimpleNamespace(
            TextSelectableByMouse=qt.TextSelectableByMouse,
        ),
    )

    QtCore = _ModuleProxy(
        _QtCore,
        Qt=qt_proxy,
        QEvent=_TypeProxy(_QtCore.QEvent, Type=SimpleNamespace(Leave=_QtCore.QEvent.Leave)),
    )
    QtGui = _ModuleProxy(
        _QtGui,
        QAction=_QtWidgets.QAction,
        QKeySequence=_TypeProxy(
            _QtGui.QKeySequence,
            StandardKey=SimpleNamespace(
                Open=_QtGui.QKeySequence.Open,
                Quit=_QtGui.QKeySequence.Quit,
            ),
        ),
        QPainter=_TypeProxy(
            _QtGui.QPainter,
            RenderHint=SimpleNamespace(Antialiasing=_QtGui.QPainter.Antialiasing),
        ),
    )
    QtWidgets = _ModuleProxy(
        _QtWidgets,
        QSizePolicy=_TypeProxy(
            _QtWidgets.QSizePolicy,
            Policy=SimpleNamespace(
                Expanding=_QtWidgets.QSizePolicy.Expanding,
                Fixed=_QtWidgets.QSizePolicy.Fixed,
            ),
        ),
        QFrame=_TypeProxy(
            _QtWidgets.QFrame,
            Shape=SimpleNamespace(
                NoFrame=_QtWidgets.QFrame.NoFrame,
                StyledPanel=_QtWidgets.QFrame.StyledPanel,
                VLine=_QtWidgets.QFrame.VLine,
            ),
            Shadow=SimpleNamespace(Sunken=_QtWidgets.QFrame.Sunken),
        ),
        QDialogButtonBox=_TypeProxy(
            _QtWidgets.QDialogButtonBox,
            StandardButton=SimpleNamespace(
                Ok=_QtWidgets.QDialogButtonBox.Ok,
                Cancel=_QtWidgets.QDialogButtonBox.Cancel,
            ),
        ),
        QTableWidget=_TypeProxy(
            _QtWidgets.QTableWidget,
            EditTrigger=SimpleNamespace(NoEditTriggers=_QtWidgets.QTableWidget.NoEditTriggers),
            SelectionMode=SimpleNamespace(NoSelection=_QtWidgets.QTableWidget.NoSelection),
        ),
        QHeaderView=_TypeProxy(
            _QtWidgets.QHeaderView,
            ResizeMode=SimpleNamespace(
                ResizeToContents=_QtWidgets.QHeaderView.ResizeToContents,
                Stretch=_QtWidgets.QHeaderView.Stretch,
            ),
        ),
        QComboBox=_TypeProxy(
            _QtWidgets.QComboBox,
            SizeAdjustPolicy=SimpleNamespace(
                AdjustToContents=_QtWidgets.QComboBox.AdjustToContents,
            ),
        ),
    )
