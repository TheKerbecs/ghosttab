"""Read the focused text field through UI Automation: text around the caret,
caret and field geometry, and whether it is a password field.

Must be called from a thread that has initialised COM
(uiautomation.UIAutomationInitializerInThread).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import uiautomation as uia

from . import win32

log = logging.getLogger(__name__)

START, END = 0, 1          # TextPatternRangeEndpoint
CHAR, LINE = 0, 3          # TextUnit_Character, TextUnit_Line
PRE_CHARS = 2000
POST_CHARS = 300
SELECTION_CHARS = 8000
EDITABLE_TYPES = {uia.ControlType.EditControl, uia.ControlType.DocumentControl,
                  uia.ControlType.ComboBoxControl, uia.ControlType.CustomControl,
                  uia.ControlType.GroupControl, uia.ControlType.PaneControl}


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w


@dataclass
class FocusContext:
    element_id: tuple
    pid: int
    app: str
    window_title: str
    preceding: str
    trailing: str
    caret: Rect
    caret_exact: bool
    field: Rect
    is_password: bool
    line_left: int = 0     # x where wrapped text lines start inside the field
    editable: bool = True
    selection: str = ""
    selection_rect: Rect | None = None      # first line of the selection
    selection_end: Rect | None = None       # last line of the selection
    hwnd: int = 0          # foreground window the element belongs to


def _rects(raw_range) -> list[Rect]:
    vals = list(raw_range.GetBoundingRectangles() or ())
    out = []
    for i in range(0, len(vals) - 3, 4):
        x, y, w, h = vals[i:i + 4]
        if w >= 0 and h > 0:
            out.append(Rect(int(x), int(y), int(w), int(h)))
    return out


def _caret_from_text(sel) -> tuple[Rect, bool] | None:
    # Bounding box of the character just before the caret -> caret at its right.
    before = sel.Clone()
    before.MoveEndpointByRange(END, sel, START)
    if before.MoveEndpointByUnit(START, CHAR, -1) == -1:
        rects = _rects(before)
        text = before.GetText(1)
        if rects and text not in ("\r", "\n"):
            r = rects[-1]
            return Rect(r.right, r.y, 1, r.h), True
    # Otherwise the character after the caret -> caret at its left.
    after = sel.Clone()
    after.MoveEndpointByRange(END, sel, START)
    if after.MoveEndpointByUnit(END, CHAR, 1) == 1:
        rects = _rects(after)
        if rects and after.GetText(1) not in ("\r", "\n"):
            r = rects[0]
            return Rect(r.x, r.y, 1, r.h), True
    # Degenerate ranges sometimes still report a rect (empty fields in Chromium).
    rects = _rects(sel)
    if rects:
        r = rects[0]
        return Rect(r.x, r.y, 1, r.h), True
    return None


def read_focus(own_pid: int) -> FocusContext | None:
    try:
        ctrl = uia.GetFocusedControl()
    except Exception:
        return None
    if ctrl is None:
        return None
    return read_control(ctrl, own_pid)


def read_control(ctrl: uia.Control, own_pid: int) -> FocusContext | None:
    try:
        pid = ctrl.ProcessId
        if pid == own_pid:
            return None
        element_id = tuple(ctrl.GetRuntimeId() or ())
        br = ctrl.BoundingRectangle
        field = Rect(br.left, br.top, br.width(), br.height())
        is_password = bool(ctrl.IsPassword)
        fg_hwnd, title, gui_tid = win32.foreground_window()
        if win32.window_pid(fg_hwnd) != pid:
            # Not the foreground app (e.g. a probe); name the element's own
            # window, and don't borrow another app's system caret.
            top = ctrl.GetTopLevelControl()
            title = top.Name if top is not None else ""
            gui_tid = 0
            fg_hwnd = 0
        app = win32.process_exe(pid)

        value_pattern = ctrl.GetPattern(uia.PatternId.ValuePattern)
        # A web page's document has a TextPattern too; a read-only Value, or
        # no Value on a non-focusable element, marks text that can't be typed into.
        if value_pattern is not None:
            editable = not value_pattern.IsReadOnly
        else:
            editable = ctrl.ControlType in EDITABLE_TYPES and bool(ctrl.IsKeyboardFocusable)

        preceding = trailing = selection_text = ""
        selection_rect = selection_end = None
        caret: tuple[Rect, bool] | None = None
        line_left = field.x + 4
        text_pattern = ctrl.GetPattern(uia.PatternId.TextPattern)
        if text_pattern is not None:
            selection = text_pattern.pattern.GetSelection()
            if selection is None or selection.Length == 0:
                return None
            sel = selection.GetElement(0)
            pre = sel.Clone()
            pre.MoveEndpointByRange(END, sel, START)
            pre.MoveEndpointByUnit(START, CHAR, -PRE_CHARS)
            preceding = pre.GetText(-1) or ""
            post = sel.Clone()
            post.MoveEndpointByRange(START, sel, END)
            post.MoveEndpointByUnit(END, CHAR, POST_CHARS)
            trailing = post.GetText(-1) or ""
            selection_text = sel.GetText(SELECTION_CHARS) or ""
            if selection_text:
                sel_rects = _rects(sel)
                if sel_rects:
                    selection_rect, selection_end = sel_rects[0], sel_rects[-1]
            caret = _caret_from_text(sel)
            line = sel.Clone()
            line.ExpandToEnclosingUnit(LINE)
            line_rects = _rects(line)
            if line_rects:
                line_left = line_rects[0].x
        else:
            if value_pattern is None or ctrl.ControlType not in EDITABLE_TYPES:
                log.debug("focused %s (%s) has no Text/Value pattern",
                          ctrl.ControlTypeName, win32.process_exe(pid))
                return None
            if value_pattern.IsReadOnly:
                return None
            preceding = value_pattern.Value or ""  # caret position unknown: assume end

        if caret is None:
            sys_caret = win32.win32_caret_rect(gui_tid) if gui_tid else None
            if sys_caret:
                caret = Rect(*sys_caret), True
            else:
                line_h = min(field.h, 24) or 18
                caret = Rect(field.x + 4, field.y + (field.h - line_h) // 2, 1, line_h), False

        return FocusContext(
            element_id=element_id, pid=pid, app=app, window_title=title,
            preceding=preceding.replace("\r\n", "\n").replace("\r", "\n"),
            trailing=trailing.replace("\r\n", "\n").replace("\r", "\n"),
            caret=caret[0], caret_exact=caret[1], field=field, is_password=is_password,
            line_left=line_left, editable=editable,
            selection=selection_text.replace("\r\n", "\n").replace("\r", "\n"),
            selection_rect=selection_rect, selection_end=selection_end, hwnd=fg_hwnd,
        )
    except Exception as exc:  # COM errors when the element disappears mid-read
        log.debug("read_focus failed: %r", exc)
        return None

