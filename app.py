r"""
app.py - Opus: a small always-on-top AI desktop pet for Windows 11.

RUN INSTRUCTIONS (Windows PowerShell)
-------------------------------------
1. Install Python 3.12 or newer from python.org. Tick "Add python.exe to PATH".
2. Install Ollama from https://ollama.com, start it, then pull the model once:
       ollama pull llama3.2
3. In the folder containing app.py, agent.py, companion.py and requirements.txt:
       pip install -r requirements.txt
4. Optional settings (environment variables):
       $env:STACKY_MODEL = "qwen3-coder:30b"      # use another installed model (larger ones are slower)
       $env:OLLAMA_API_KEY = "..."                # turns on web search
       $env:STACKY_HOTKEY = "ctrl+alt+space"      # open the prompt box
       $env:STACKY_MOVE_HOTKEY = "ctrl+alt+m"     # move mode: follow the mouse, click or flick to place
       $env:STACKY_DUCK_HOTKEY = "ctrl+alt+d"     # rubber duck mode
5. Start Opus:
       python app.py         (or double-click app.pyw to run without a console window)

CONTROLS
    Ask ..................... double-click the pet, Ctrl+Alt+Space, or the pencil button
    Talk instead of typing .. the microphone button in the prompt box (Windows voice typing)
    Move .................... drag it, or press Ctrl+Alt+M, move the mouse and click
    Flick ................... a fast drag sends it to that screen edge; flick down to dismiss it
    Menu .................... right-click the pet or the tray icon, or the chevron button

COMMANDS (type them in the prompt box)
    /notes <question>     answer from your own files, with the file names
    /clip <words>         search your clipboard history, e.g. /clip that error yesterday
    /script <task>        write a small Python script; it runs only after you approve it
    /timer <minutes> <label>
    /duck                 rubber duck mode on or off

TELL OPUS HOW A COMMAND WENT
    python opus_run.py npm run build     # a cheer on success, a short diagnosis on failure

Everything Opus records stays on this computer, under %APPDATA%\Opus.
"""
from __future__ import annotations

import ctypes
import logging
import math
import os
import random
import re
import signal
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from html import escape
from pathlib import Path

from PyQt6.QtCore import (
    QAbstractNativeEventFilter,
    QEasingCurve,
    QObject,
    QPointF,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QActionGroup,
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSystemTrayIcon,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from agent import (
    AskRequest,
    Conversation,
    HealthCheck,
    ModelTask,
    Tool,
    ToolBox,
    compose_system_prompt,
    load_settings,
    schema,
    web_search_tool,
)
from companion import (
    CODE_EXTENSIONS,
    FAILURE_SYSTEM_PROMPT,
    NOTES_INSTRUCTIONS,
    REVIEW_SYSTEM_PROMPT,
    SCRIPT_SYSTEM_PROMPT,
    TOPIC_SYSTEM_PROMPT,
    VK_ESCAPE,
    VK_LBUTTON,
    ClipboardStore,
    Config,
    EventInbox,
    FolderWatcher,
    HabitTracker,
    KnowledgeBase,
    Personality,
    PetNeeds,
    SystemStats,
    build_review_prompt,
    builtin_personality,
    calculate,
    describe_event,
    extract_python,
    format_clips,
    format_duration,
    format_hits,
    key_down,
    load_personalities,
    network,
    next_calendar_event,
    parse_review,
    parse_topics,
    personalities_dir,
    play_sound,
    run_script,
    save_script,
    start_voice_typing,
    topic_prompt,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("stacky")

# --------------------------------------------------------------------------- #
# Character: vector robot, drawn at any resolution
# --------------------------------------------------------------------------- #
# The pet is drawn with antialiased QPainterPath shapes in a 100 x 122 design
# space, then mapped onto whatever rectangle it is given. Nothing is pixel-based,
# so it stays sharp at every window size and display scale. The body is glass;
# its layers never change, so RobotArt renders them once per size into a pixmap
# and each frame only paints the glow, the eyes and the sparkle on top.
DESIGN_W, DESIGN_H = 100.0, 122.0
HEADROOM = 12.0                 # room above the head for the antenna; body coordinates start below it
PET_W, PET_H = 66, 81           # on-screen size, about the same 100:122 proportion (small desk pet)

# Colours. Tweak here; the drawing code only refers to these names.
INK = "#1c2a6e"                 # outline
GLASS_LIGHT = "#d4e3ff"         # glass body: light at the top-left ...
BODY_TOP = "#86b0ff"
BODY_BOTTOM = "#3a5fe6"         # ... deeper at the bottom-right
VISOR = "#151b33"
VISOR_EDGE = "#2c3f8f"
GLYPH = "#8ff0ff"               # resting glow: eyes, chest prompt, halo, antenna tip. The status shifts it.
EYE_SIZE = 1.3                  # eye size multiplier
EYE_BLUE_TOP = "#9fd0ff"        # eyeball colours: light blue highlight to deep blue
EYE_BLUE_BOTTOM = "#1d4fb8"
CHEST = "#1b2440"

# The pet's looks. The status picks one (see the controller). When nothing is happening,
# the pet's needs, Ollama's health and the battery pick its resting look.
#   idle      resting, the default
#   curious   wide eyes; shown when someone hovers or clicks the pet, and when it asks a question
#   thinking  confused; a head tilt, a question mark, and a squiggly eye
#   done      happy; arched eyes and a smile
#   error     frustrated; red body, angry brows, and a wavy mouth
#   sleepy    heavy lids and floating z's; heavy CPU use has worn it out
#   bored     half-lidded side glance and a flat mouth; you have been away a long time
#   hungry    big hopeful eyes and an open mouth; feed it from the menu
#   worried   brows raised in the middle; Ollama is down or the battery is low
EXPRESSIONS = ("idle", "curious", "thinking", "done", "error", "sleepy", "bored", "hungry", "worried")

MAX_PROMPT_CHARS = 4000         # longest question sent to the model


def _mix(a: str | QColor, b: str | QColor, t: float) -> QColor:
    """Blend colour a towards colour b by t (0 is all a, 1 is all b)."""
    ca, cb = QColor(a), QColor(b)
    return QColor(
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t),
    )


def _alpha(colour: str | QColor, alpha: int) -> QColor:
    c = QColor(colour)
    c.setAlpha(alpha)
    return c


def _head_path() -> QPainterPath:
    """Cloud-shaped head: a rounded block with three bumps along the top."""
    head = QPainterPath()
    head.addRoundedRect(QRectF(16, 14, 68, 52), 24, 24)
    for x, y, d in ((22, 8, 30), (44, 2, 34), (66, 8, 28)):
        bump = QPainterPath()
        bump.addEllipse(QRectF(x, y, d, d))
        head = head.united(bump)
    return head


def _rounded(x: float, y: float, w: float, h: float, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), radius, radius)
    return path


def _stroke(p: QPainter, colour: QColor, width: float) -> None:
    p.setPen(QPen(colour, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)


def _fill(p: QPainter, colour: QColor) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)


EYE_LEFT_X, EYE_RIGHT_X, EYE_Y = 34.0, 62.0, 41.0


def _chevron(p: QPainter, x: float, y: float, size: float, glyph: QColor, pointing_left: bool = False) -> None:
    """Codex's ">" eye: a chevron, pointing right by default."""
    _stroke(p, glyph, 3.0)
    d = -1.0 if pointing_left else 1.0
    path = QPainterPath(QPointF(x - d * size, y - size))
    path.lineTo(QPointF(x + d * size, y))
    path.lineTo(QPointF(x - d * size, y + size))
    p.drawPath(path)


def _visor_glyphs(p: QPainter, expression: str, blink: bool, glyph: QColor) -> None:
    """Codex's eyes on the visor: a ">" and a "_". Each look changes the eyes and the mouth."""
    lx, rx, y = EYE_LEFT_X, EYE_RIGHT_X, EYE_Y

    if blink:
        _stroke(p, glyph, 3.0)
        p.drawLine(QPointF(lx - 4, y), QPointF(lx + 4, y))
        p.drawLine(QPointF(rx - 4, y), QPointF(rx + 4, y))
        return

    if expression == "done":
        # Happy: "^ ^" eyes and a smile
        _stroke(p, glyph, 3.0)
        for x in (lx, rx):
            arc = QPainterPath(QPointF(x - 4, y + 3))
            arc.lineTo(x, y - 4)
            arc.lineTo(x + 4, y + 3)
            p.drawPath(arc)
        mouth = QPainterPath(QPointF(44, 48))
        mouth.quadTo(49, 53, 54, 48)
        p.drawPath(mouth)
        return

    if expression == "error":
        # Frustrated: "> <" eyes pressed together, and a wavy mouth
        _chevron(p, lx, y, 4.0, glyph)
        _chevron(p, rx, y, 4.0, glyph, pointing_left=True)
        _stroke(p, glyph, 2.4)
        wave = QPainterPath(QPointF(38, 51))
        wave.quadTo(42, 47, 46, 51)
        wave.quadTo(50, 55, 54, 51)
        wave.quadTo(58, 47, 62, 51)
        p.drawPath(wave)
        return

    # Everything else: Codex's main eyes, ">" and "_"
    _chevron(p, lx, y, 5.0, glyph)
    _stroke(p, glyph, 3.0)
    if expression in ("idle", "curious"):
        # The resting right eye is a small curved wink, as in the reference
        wink = QPainterPath(QPointF(rx - 4, y))
        wink.quadTo(rx + 1, y + 4, rx + 6, y)
        p.drawPath(wink)
    else:
        p.drawLine(QPointF(rx - 4, y + 4), QPointF(rx + 6, y + 4))

    if expression == "sleepy":
        _stroke(p, glyph, 2.2)
        p.drawEllipse(QPointF(49, 50), 2.2, 2.6)
    elif expression == "bored":
        _stroke(p, glyph, 2.4)
        p.drawLine(QPointF(44, 49), QPointF(54, 49))
    elif expression == "hungry":
        _stroke(p, glyph, 2.4)
        p.drawEllipse(QPointF(49, 50), 3.0, 3.2)
    elif expression == "worried":
        _stroke(p, glyph, 2.4)
        frown = QPainterPath(QPointF(44, 51))
        frown.quadTo(49, 47, 54, 51)
        p.drawPath(frown)
    elif expression == "thinking":
        pass  # no mouth; _thought_mark puts a question mark beside the head
    else:
        # idle and curious: a small smile
        _stroke(p, glyph, 2.4)
        smile = QPainterPath(QPointF(44, 48))
        smile.quadTo(49, 51, 54, 48)
        p.drawPath(smile)


def _eye_anchors(expression: str) -> tuple[QPointF, QPointF]:
    """Points on the left and right eye glyphs where the catchlights sit."""
    lx, rx, y = EYE_LEFT_X, EYE_RIGHT_X, EYE_Y
    if expression == "done":
        return QPointF(lx, y - 4), QPointF(rx, y - 4)              # tops of the "^ ^"
    if expression == "error":
        return QPointF(lx, y - 2), QPointF(rx, y - 2)              # upper strokes of the "> <"
    right = QPointF(rx + 1, y + 2) if expression in ("idle", "curious") else QPointF(rx + 1, y + 4)
    return QPointF(lx, y - 2.5), right                             # upper stroke of ">", then the wink or "_"


def _thought_mark(p: QPainter) -> None:
    """The question mark beside the head while thinking."""
    _stroke(p, QColor(INK), 2.6)
    mark = QPainterPath(QPointF(82, 15))
    mark.quadTo(82, 8, 88, 8)
    mark.quadTo(94, 8, 94, 14)
    mark.quadTo(94, 18, 88, 19)
    mark.lineTo(88, 23)
    p.drawPath(mark)
    _fill(p, QColor(INK))
    p.drawEllipse(QPointF(88, 27.5), 1.8, 1.8)


def _soft_glow(p: QPainter, centre: QPointF, rx: float, ry: float, colour: QColor, alpha: int,
               inner: float = 0.0) -> None:
    """An elliptical radial glow: full `alpha` out to `inner` (a fraction of the radius), then fading to nothing."""
    grad = QRadialGradient(QPointF(0, 0), 1.0)
    grad.setColorAt(inner, _alpha(colour, alpha))
    grad.setColorAt(1.0, _alpha(colour, 0))
    p.save()
    p.translate(centre)
    p.scale(rx, ry)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(grad))
    p.drawEllipse(QPointF(0, 0), 1.0, 1.0)
    p.restore()


def _design_image(size: QSize, draw) -> QImage:
    """A transparent image of `size` device pixels, with draw(painter) called in design units."""
    image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.scale(size.width() / DESIGN_W, size.height() / DESIGN_H)
    p.translate(0, HEADROOM)
    draw(p)
    p.end()
    return image


def _blurred(image: QImage, radius: float) -> QImage:
    """A blurred copy of image, the same size. Only used when building caches, never per frame."""
    item = QGraphicsPixmapItem(QPixmap.fromImage(image))
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius)
    effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
    item.setGraphicsEffect(effect)
    scene = QGraphicsScene()
    scene.addItem(item)
    out = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    scene.render(p, QRectF(out.rect()), QRectF(out.rect()))
    p.end()
    return out


def _tint(mask: QImage, colour: QColor) -> QImage:
    """mask in a single colour, keeping the mask's alpha."""
    image = QImage(mask.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(colour)
    p = QPainter(image)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    p.drawImage(0, 0, mask)
    p.end()
    return image


class RobotArt:
    """
    Paints the robot in layers, bottom to top:

        state halo                  blurred silhouette mask, tinted
        glass body                  one cached pixmap: drop shadow, glass fill, inner rims and outlines,
                                    seam, rivets and vents, chest plate, visor, antenna stalk, sheen
                                    and specular highlights
        glow light                  cached mask, tinted: visor bleed, caustics, chest prompt glow
        chest prompt, eyes          eye glow is a blurred copy of the glyphs, cached per look and tinted
        catchlights, question mark, antenna tip, sparkle sweep

    Everything cached is rendered once per device size; each frame only draws the images
    through the body's transform plus a few small strokes. The glow colour follows the
    status; the glass keeps its tint.
    """

    SUPERSAMPLE = 2              # the cached body is rendered at twice the device resolution so it stays crisp as it bobs
    BREATH = 0.01                # breathing scale, as a fraction of the body
    RIM = 2.0                    # depth of the inner edges; the outline covers about half of it
    SHADOW_OFFSET = QPointF(1.5, 2.5)
    HALO_OPACITY = 0.5
    ANTENNA_BASE = QPointF(60.5, 3.0)
    ANTENNA_TIP = QPointF(57.0, -5.5)

    def __init__(self):
        self._parts = [                                     # back to front
            _rounded(12, 62, 18, 28, 9), _rounded(70, 62, 18, 28, 9),    # arms
            _rounded(32, 84, 15, 17, 7), _rounded(53, 84, 15, 17, 7),    # legs
            _rounded(30, 58, 40, 32, 13),                                 # torso
            _head_path(),
        ]
        self._torso, self._head = self._parts[4], self._parts[5]
        self._silhouette = QPainterPath()
        for part in self._parts:
            self._silhouette = self._silhouette.united(part)
        self._visor = _rounded(24, 26, 52, 30, 11)
        self._chest = _rounded(40, 66, 20, 12, 4)
        self._head_glass = self._head.subtracted(self._visor)
        self._body_glass = self._silhouette.subtracted(self._visor)
        self._prompt = QPainterPath(QPointF(45, 70))      # the ">_" on the chest plate
        self._prompt.lineTo(49, 72)
        self._prompt.lineTo(45, 74)
        self._prompt.moveTo(51, 75)
        self._prompt.lineTo(56, 75)
        self._frame = QRectF(0, -HEADROOM, DESIGN_W, DESIGN_H)   # the whole design space, where cached images go

        self._size = QSize()                # device size the caches below were built for
        self._px = 1.0                      # device pixels per design unit at that size
        self._body = QPixmap()
        self._halo = QImage()               # masks, in white; tinted with the glow colour when drawn
        self._light = QImage()
        self._eye_glow: dict[str, QImage] = {}
        self._tinted: dict[tuple[str, int], QImage] = {}

    # -- per frame ------------------------------------------------------------ #
    def paint(self, p: QPainter, rect: QRectF, expression: str = "idle", blink: bool = False,
              lift: float = 0.0, squash: float = 0.0, gaze: float = 0.0, tilt: float = 0.0,
              glow: str | QColor = GLYPH, look: QPointF | None = None, breath: float = 0.0,
              sweep: float = -1.0) -> None:
        """
        Paint the robot into `rect`.

        lift    vertical offset in design units (positive is down, negative is up)
        squash  small squash/stretch around the feet, as a fraction (e.g. 0.02)
        gaze    horizontal shift of the visor glyphs, in design units
        tilt    rotation in degrees around the feet, for confusion and trembling
        glow    colour of the eyes, halo, visor light and antenna tip
        look    cursor direction, -1 to 1 on each axis; the catchlights drift toward it
        breath  -1 to 1; a slow scale pulse of the body, separate from the bob
        sweep   progress of the sparkle sweep, 0 to 1; any other value draws none
        """
        if expression not in EXPRESSIONS:
            expression = "idle"
        glow = QColor(glow)
        look = QPointF() if look is None else look
        core = _mix(glow, "#ffffff", 0.35)   # glowing strokes are whiter than their glow
        dpr = p.device().devicePixelRatioF()
        size = QSize(max(1, round(rect.width() * dpr)), max(1, round(rect.height() * dpr)))
        if size != self._size:
            self._build(size)

        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.translate(rect.left(), rect.top())
        p.scale(rect.width() / DESIGN_W, rect.height() / DESIGN_H)
        p.translate(0, HEADROOM)

        # Ground shadow, soft-edged, which narrows while the robot hops up
        shrink = max(0.6, 1.0 + min(0.0, lift) / 30.0)
        _soft_glow(p, QPointF(50, 104), 27 * shrink, 4.5 * shrink, QColor(0, 0, 0), 80, 0.3)

        # The body squashes around the feet and breathes, moves with the bob, and tilts
        grow = 1.0 + self.BREATH * breath
        p.translate(50, 110)
        p.scale((1.0 + squash) * grow, (1.0 - squash) * grow)
        p.translate(-50, -110 + lift)
        p.translate(50, 100)
        p.rotate(tilt)
        p.translate(-50, -100)

        p.setOpacity(self.HALO_OPACITY)
        p.drawImage(self._frame, self._tinted_mask("halo", self._halo, glow))
        p.setOpacity(1.0)
        p.drawPixmap(self._frame, self._body, QRectF(self._body.rect()))
        p.drawImage(self._frame, self._tinted_mask("light", self._light, glow))
        p.strokePath(self._prompt, QPen(core, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                        Qt.PenJoinStyle.RoundJoin))

        # Eyes: a blurred copy beneath for the glow, the glyphs, then catchlights that follow the cursor
        p.save()
        p.translate(gaze, 0.0)  # the eyes slide toward the cursor; the visor itself stays put
        key = "blink" if blink else expression
        p.drawImage(self._frame, self._tinted_mask(key, self._eye_glow_mask(key, expression, blink), glow))
        _visor_glyphs(p, expression, blink, core)
        if not blink:
            for anchor in _eye_anchors(expression):
                dot = anchor + QPointF(look.x() * 1.4, look.y() * 1.1)
                _fill(p, QColor(255, 255, 255, 70))
                p.drawEllipse(dot, 1.9, 1.9)
                _fill(p, QColor(255, 255, 255, 240))
                p.drawEllipse(dot, 0.9, 0.9)
        p.restore()
        if expression == "thinking":
            _thought_mark(p)

        # Antenna tip: a glowing bulb that pulses with the breath
        tip = self.ANTENNA_TIP
        _soft_glow(p, tip, 6.0 + breath * 0.6, 6.0 + breath * 0.6, glow, round(150 + breath * 30), 0.2)
        _fill(p, core)
        p.drawEllipse(tip, 2.3, 2.3)
        _fill(p, QColor(255, 255, 255, 230))
        p.drawEllipse(tip + QPointF(-0.7, -0.8), 0.75, 0.75)

        if 0.0 <= sweep < 1.0:
            self._paint_sweep(p, sweep)
        p.restore()

    def _paint_light(self, p: QPainter) -> None:
        """
        Mask, in white, of the light the glow casts: a faint bleed into the glass around the visor,
        caustics under it that reflect onto the chest plate, and the glow of the chest prompt.
        """
        white = QColor(255, 255, 255)
        p.save()
        p.setClipPath(self._head_glass)
        _soft_glow(p, QPointF(50, 41), 40, 25, white, 38, 0.55)
        p.setClipPath(self._body_glass)
        _soft_glow(p, QPointF(50, 62), 22, 12, white, 48)
        p.setClipPath(self._chest)
        reflection = QLinearGradient(QPointF(0, 66), QPointF(0, 74))
        reflection.setColorAt(0.0, _alpha(white, 95))
        reflection.setColorAt(1.0, _alpha(white, 0))
        p.fillRect(QRectF(40, 66, 20, 12), QBrush(reflection))
        p.restore()
        p.strokePath(self._prompt, QPen(_alpha(white, 70), 4.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                        Qt.PenJoinStyle.RoundJoin))

    def _paint_sweep(self, p: QPainter, progress: float) -> None:
        """A soft diagonal band of light that crosses the body once."""
        eased = progress * progress * (3.0 - 2.0 * progress)
        x = -25.0 + 150.0 * eased
        band = QLinearGradient(QPointF(x - 9, 0), QPointF(x + 9, 0))
        band.setColorAt(0.0, QColor(255, 255, 255, 0))
        band.setColorAt(0.5, QColor(255, 255, 255, 95))
        band.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.save()
        p.setClipPath(self._silhouette)
        p.translate(50, 50)
        p.rotate(25)
        p.translate(-50, -50)
        p.fillRect(QRectF(x - 9, -60, 18, 220), QBrush(band))
        p.restore()

    # -- caches --------------------------------------------------------------- #
    def _tinted_mask(self, name: str, mask: QImage, colour: QColor) -> QImage:
        """mask tinted with colour, remembered so a steady colour costs nothing per frame."""
        key = (name, colour.rgba())
        image = self._tinted.get(key)
        if image is None:
            if len(self._tinted) > 32:   # a colour transition makes a new tint every frame for about a second
                self._tinted.clear()
            image = self._tinted[key] = _tint(mask, colour)
        return image

    def _eye_glow_mask(self, key: str, expression: str, blink: bool) -> QImage:
        mask = self._eye_glow.get(key)
        if mask is None:
            sharp = _design_image(self._size, lambda q: _visor_glyphs(q, expression, blink, QColor("white")))
            mask = self._eye_glow[key] = _blurred(sharp, 2.6 * self._px)
        return mask

    def _build(self, size: QSize) -> None:
        """Render the layers that never change, and the halo mask, for this device size."""
        self._size = QSize(size)
        self._px = size.width() / DESIGN_W
        self._eye_glow.clear()
        self._tinted.clear()
        silhouette = _design_image(size, lambda q: q.fillPath(self._silhouette, QColor("white")))
        self._halo = _blurred(silhouette, 7.0 * self._px)
        self._light = _design_image(size, self._paint_light)
        shadow = _tint(_blurred(silhouette, 2.5 * self._px), QColor(0, 0, 0))

        def glass(p: QPainter) -> None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.setOpacity(0.38)
            p.drawImage(self._frame.translated(self.SHADOW_OFFSET), shadow)
            p.setOpacity(1.0)
            # The glass is see-through, so the shadow must not show inside it
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillPath(self._silhouette, QColor(0, 0, 0))
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            self._paint_glass(p)

        self._body = QPixmap.fromImage(_design_image(size * self.SUPERSAMPLE, glass))

    def _paint_glass(self, p: QPainter) -> None:
        """The static layers, in design units: glass, rims, details, chest plate, visor, antenna, highlights."""
        # Glass: one translucent gradient across the whole body, light at the top-left, deeper at the bottom-right
        fill = QLinearGradient(QPointF(14, 4), QPointF(86, 100))
        fill.setColorAt(0.0, _alpha(GLASS_LIGHT, 215))
        fill.setColorAt(0.45, _alpha(BODY_TOP, 228))
        fill.setColorAt(1.0, _alpha(BODY_BOTTOM, 242))
        p.fillPath(self._silhouette, QBrush(fill))

        # Each part gets a bright inner edge on top and a darker one underneath, to suggest
        # thickness, then a thin outline. Parts in front hide the edges of the parts behind.
        outline = QPen(_alpha(INK, 200), 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                       Qt.PenJoinStyle.RoundJoin)
        everything = QPainterPath()
        everything.addRect(QRectF(-10, -HEADROOM - 10, DESIGN_W + 20, DESIGN_H + 20))
        for i, part in enumerate(self._parts):
            front = QPainterPath()
            for other in self._parts[i + 1:]:
                front = front.united(other)
            p.save()
            p.setClipPath(everything.subtracted(front))
            p.fillPath(part.subtracted(part.translated(0, self.RIM)), QColor(255, 255, 255, 160))
            p.fillPath(part.subtracted(part.translated(0, -self.RIM)), _alpha(INK, 115))
            p.strokePath(part, outline)
            p.restore()

        # Seam: a light line just under the head, so its edge reads as a groove into the torso
        p.save()
        p.setClipPath(self._torso.subtracted(self._head))
        p.strokePath(self._head.translated(0, 1.3), QPen(QColor(255, 255, 255, 80), 0.9))
        p.restore()

        # Rivets beside the visor and on the arms, vents under the chest plate; all low contrast
        for c in (QPointF(20, 41), QPointF(80, 41), QPointF(21, 82), QPointF(79, 82)):
            _fill(p, _alpha(INK, 70))
            p.drawEllipse(c, 1.3, 1.3)
            _fill(p, QColor(255, 255, 255, 90))
            p.drawEllipse(c + QPointF(-0.4, -0.4), 0.55, 0.55)
        for y in (81.5, 84.5):
            _stroke(p, _alpha(INK, 80), 1.0)
            p.drawLine(QPointF(45, y), QPointF(55, y))
            _stroke(p, QColor(255, 255, 255, 55), 0.8)
            p.drawLine(QPointF(45, y + 0.9), QPointF(55, y + 0.9))

        # Chest plate: a dark inset screen, catching light on its lower inner edge
        plate = QLinearGradient(QPointF(0, 66), QPointF(0, 78))
        plate.setColorAt(0.0, QColor("#0f1530"))
        plate.setColorAt(1.0, QColor("#26325e"))
        p.setPen(QPen(_alpha(INK, 220), 1.2))
        p.setBrush(QBrush(plate))
        p.drawPath(self._chest)
        p.fillPath(self._chest.subtracted(self._chest.translated(0, -1.0)), QColor(255, 255, 255, 50))

        # Visor: a dark glass panel with a reflection streak and a lit lower rim
        lens = QLinearGradient(QPointF(0, 26), QPointF(0, 56))
        lens.setColorAt(0.0, _alpha("#232c55", 245))
        lens.setColorAt(0.55, _alpha(VISOR, 245))
        lens.setColorAt(1.0, _alpha("#0a0e1f", 250))
        p.setPen(QPen(QColor(VISOR_EDGE), 1.6))
        p.setBrush(QBrush(lens))
        p.drawPath(self._visor)
        p.save()
        p.setClipPath(self._visor)
        _fill(p, QColor(255, 255, 255, 26))
        p.drawPolygon(QPolygonF([QPointF(37, 26), QPointF(47, 26), QPointF(35, 56), QPointF(25, 56)]))
        _fill(p, QColor(255, 255, 255, 18))
        p.drawPolygon(QPolygonF([QPointF(50, 26), QPointF(53, 26), QPointF(41, 56), QPointF(38, 56)]))
        p.restore()
        p.fillPath(self._visor.subtracted(self._visor.translated(0, -1.4)), _alpha("#6f86d9", 90))

        # Antenna stalk with a glint along it, and a collar where it meets the head
        stalk = QPainterPath(self.ANTENNA_BASE)
        stalk.quadTo(QPointF(61, -1.5), self.ANTENNA_TIP)
        p.strokePath(stalk, QPen(_alpha(INK, 210), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.strokePath(stalk, QPen(QColor(255, 255, 255, 110), 0.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setPen(QPen(_alpha(INK, 200), 1.2))
        p.setBrush(QColor(BODY_TOP))
        p.drawEllipse(self.ANTENNA_BASE, 3.2, 1.7)

        # Specular: a broad soft sheen across the upper third, then sharp highlights on the head and shoulders
        p.save()
        p.setClipPath(self._silhouette)
        _soft_glow(p, QPointF(48, 12), 42, 22, QColor(255, 255, 255), 75)
        p.restore()
        _fill(p, QColor(255, 255, 255, 230))
        for centre, rx, ry, angle in ((QPointF(30, 14.5), 4.4, 1.9, -35),
                                      (QPointF(15.8, 67.5), 1.9, 1.0, -60),
                                      (QPointF(76.2, 65.4), 1.9, 1.0, -30)):
            p.save()
            p.translate(centre)
            p.rotate(angle)
            p.drawEllipse(QPointF(0, 0), rx, ry)
            p.restore()


def make_icon(expression: str = "idle") -> QIcon:
    """Render the robot as a tray icon."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    width = 64 * DESIGN_W / DESIGN_H
    RobotArt().paint(painter, QRectF((64 - width) / 2, 0, width, 64), expression)
    painter.end()
    return QIcon(pixmap)


# --------------------------------------------------------------------------- #
# Styling
# --------------------------------------------------------------------------- #
STYLE = """
QFrame#bubble {
    background-color: rgba(28, 24, 44, 235);
    border: 1px solid #5a4f80;
    border-radius: 12px;
}
QLabel { color: #ece8ff; font-family: 'Segoe UI'; font-size: 10pt; background: transparent; }
QLabel#title { color: #b9aee6; font-size: 9pt; }
QLabel#hint { color: #8c82b5; font-size: 8pt; }
QTextBrowser {
    background: transparent; border: none; color: #f4f1ff;
    font-family: 'Segoe UI'; font-size: 10pt;
}
QLineEdit {
    background: #2a2540; color: #f4f1ff; border: 1px solid #5a4f80; border-radius: 8px;
    padding: 6px 8px; font-family: 'Segoe UI'; font-size: 10pt;
}
QLineEdit:focus { border: 1px solid #b197ff; }
QPushButton {
    background: #3b3460; color: #f4f1ff; border: none; border-radius: 6px; padding: 4px 10px;
}
QPushButton:hover { background: #4b4270; }
QPushButton#close { padding: 0px; font-size: 12pt; }
"""


def place_near(widget: QWidget, anchor: QRect, gap: int = 10) -> None:
    """Position a bubble above the anchor, or below it if there is no room, keeping it on screen."""
    screen = QApplication.screenAt(anchor.center()) or QApplication.primaryScreen()
    area = screen.availableGeometry()
    w, h = widget.width(), widget.height()

    x = anchor.center().x() - w // 2
    y = anchor.top() - h - gap
    if y < area.top():
        y = anchor.bottom() + gap
    x = max(area.left() + 4, min(x, area.right() - w - 4))
    y = max(area.top() + 4, min(y, area.bottom() - h - 4))
    widget.move(x, y)


# --------------------------------------------------------------------------- #
# Pet window
# --------------------------------------------------------------------------- #
def drag_velocity(samples: list[tuple[float, QPoint]], window: float = 0.1) -> tuple[float, float]:
    """Pointer velocity in pixels per second over the last `window` seconds of (time, position) samples."""
    if len(samples) < 2:
        return 0.0, 0.0
    end_t, end_p = samples[-1]
    recent = [(t, p) for t, p in samples if end_t - t <= window]
    start_t, start_p = recent[0] if len(recent) >= 2 else samples[-2]
    dt = end_t - start_t
    if dt <= 0.005:
        return 0.0, 0.0
    return (end_p.x() - start_p.x()) / dt, (end_p.y() - start_p.y()) / dt


class PetWidget(QWidget):
    """The robot pet: frameless, translucent, always on top, draggable and flickable."""

    moved = pyqtSignal()
    ask_requested = pyqtSignal()
    context_menu_requested = pyqtSignal(QPoint)
    clicked = pyqtSignal()
    flicked = pyqtSignal(float, float)   # release velocity (pixels per second) after a fast drag

    # Idle motion is a slow sine bob. BOB_SPEED is radians per frame, about 2.2 s per cycle.
    BOB_SPEED = 0.095
    BOB_AMPLITUDE = 2.2          # design units; a little higher when happy
    FRAME_MS = 33                # about 30 fps; vector drawing is cheap at this size
    DRAG_THRESHOLD = 4
    HOP_FRAMES = 24              # about 0.8 s for one hop
    HOP_HEIGHT = 9.0             # design units
    GAZE_RANGE = 2.2             # how far the visor eyes slide toward the cursor
    BUSY_SPEED_FACTOR = 2.4      # the bob runs this many times faster while the agent is running
    SLEEPY_SPEED_FACTOR = 0.5    # and this much slower while the pet is sleepy
    FLICK_SPEED = 1800.0         # release speed, in pixels per second, that counts as a flick
    BREATH_SPEED = 0.046         # radians per frame, about 4.5 s per breath; separate from the bob
    SWEEP_FRAMES = 22            # the sparkle sweep crosses the body in about 0.7 s
    GLOW_EASE = 0.12             # fraction of the way to the status colour per frame

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool  # keeps the pet off the taskbar
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("Opus")
        self.setToolTip("Opus: click to open the chat, double-click to ask, right-click for the menu")
        self.setFixedSize(PET_W, PET_H)

        self._base_expression = "idle"   # the look the status asks for
        self._expression = "idle"        # what is drawn now; differs from base during a short reaction
        self._glow = QColor(GLYPH)       # eyes, halo and antenna; eases toward the status colour
        self._glow_target = QColor(GLYPH)
        self._blink = False
        self._frame = 0
        self._phase = 0.0                # bob phase, advanced every frame so speed changes never jump
        self._press_global: QPoint | None = None
        self._drag_offset: QPoint | None = None
        self._dragging = False
        self._drag_samples: list[tuple[float, QPoint]] = []
        self._swallow_click = False
        self._gaze = 0.0
        self._look = QPointF()           # cursor direction, -1 to 1 on each axis, for the catchlights
        self._breath = 0.0               # breathing phase
        self._hop_start = -10_000        # frame number when the last hop began
        self._sweep_start = -10_000      # frame number when the last sparkle sweep began
        self._bob_speed = self.BOB_SPEED
        self._offline = False
        self._art = RobotArt()

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)
        self._anim.start(self.FRAME_MS)

        self._blink_timer = QTimer(self)
        self._blink_timer.setSingleShot(True)
        self._blink_timer.timeout.connect(self._toggle_blink)
        self._blink_timer.start(random.randint(2500, 5000))

        self._sparkle_timer = QTimer(self)
        self._sparkle_timer.setSingleShot(True)
        self._sparkle_timer.timeout.connect(self._sparkle)
        self._sparkle_timer.start(random.randint(4000, 8000))

        self._revert_timer = QTimer(self)
        self._revert_timer.setSingleShot(True)
        self._revert_timer.timeout.connect(self._restore_base)

    @property
    def expression(self) -> str:
        """The base look, which is what the status asked for."""
        return self._base_expression

    def set_expression(self, name: str) -> None:
        """Set the base look for the current status. Any short reaction in progress ends."""
        if name not in EXPRESSIONS:
            name = "idle"
        self._base_expression = name
        self._expression = name
        self._revert_timer.stop()
        self.update()

    def react(self, expression: str, hold_ms: int = 1400, hop: bool = True) -> None:
        """Show a look for a moment, then go back to the base look."""
        if expression not in EXPRESSIONS:
            return
        self._expression = expression
        self._revert_timer.start(hold_ms)
        if hop:
            self.hop()
        self.update()

    def react_curious(self, hold_ms: int = 1400) -> None:
        """Interaction (hover or click) makes the pet curious for a moment."""
        self.react("curious", hold_ms)

    def swallow_next_click(self) -> None:
        """The next left click only drops the pet (move mode), so it should not open the chat."""
        self._swallow_click = True

    def set_offline_badge(self, offline: bool) -> None:
        self._offline = offline
        self.update()

    def set_glow(self, colour: str) -> None:
        """Shift the glow (eyes, halo, visor light, antenna tip) toward a status colour. The glass keeps its tint."""
        self._glow_target = QColor(colour)

    def _restore_base(self) -> None:
        self._expression = self._base_expression
        self.update()

    # -- animation -------------------------------------------------------- #
    def _tick(self) -> None:
        self._frame += 1
        factor = self.SLEEPY_SPEED_FACTOR if self._expression == "sleepy" else 1.0
        self._phase += self._bob_speed * factor
        self._breath += self.BREATH_SPEED
        # The eyes follow the cursor anywhere on screen, not only over the pet
        centre = self.mapToGlobal(self.rect().center())
        cursor = QCursor.pos()
        self._look = QPointF(max(-1.0, min(1.0, (cursor.x() - centre.x()) / 400.0)),
                             max(-1.0, min(1.0, (cursor.y() - centre.y()) / 400.0)))
        self._gaze = self._look.x() * self.GAZE_RANGE
        # The glow eases toward the status colour
        self._glow = _mix(self._glow, self._glow_target, self.GLOW_EASE)
        self.update()

    def _sparkle(self) -> None:
        """Start a highlight sweep across the body, and schedule the next one a few seconds later."""
        self._sweep_start = self._frame
        self._sparkle_timer.start(random.randint(5000, 9000))

    def _tilt(self) -> float:
        """Degrees of body rotation: a slow head tilt when confused, a tremble when upset, a sway when sleepy."""
        if self._expression == "thinking":
            return math.sin(self._frame * 0.08) * 7.0
        if self._expression == "error":
            return math.sin(self._frame * 0.9) * 1.5
        if self._expression == "curious":
            return 4.0
        if self._expression == "sleepy":
            return math.sin(self._frame * 0.03) * 3.0
        return 0.0

    def hop(self) -> None:
        """Start one short hop. The expression is left alone."""
        self._hop_start = self._frame

    def set_busy(self, busy: bool) -> None:
        """Speed up the idle bob while the agent is working, so the pet visibly looks busy."""
        self._bob_speed = self.BOB_SPEED * (self.BUSY_SPEED_FACTOR if busy else 1.0)

    def _hop_height(self) -> float:
        """Arc of the hop started by hop(), in design units. Zero when no hop is running."""
        progress = (self._frame - self._hop_start) / self.HOP_FRAMES
        if not 0.0 <= progress < 1.0:
            return 0.0
        return math.sin(math.pi * progress) * self.HOP_HEIGHT

    def _toggle_blink(self) -> None:
        # One timer alternates between "eyes open, wait a while" and "eyes closed, brief".
        self._blink = not self._blink
        self._blink_timer.start(140 if self._blink else random.randint(2500, 5000))
        self.update()

    def paintEvent(self, event):
        amplitude = 4.0 if self._expression == "done" else self.BOB_AMPLITUDE
        lift = math.sin(self._phase) * amplitude - self._hop_height()
        squash = math.sin(self._phase + 1.0) * 0.012
        sweep = (self._frame - self._sweep_start) / self.SWEEP_FRAMES
        painter = QPainter(self)
        self._art.paint(painter, QRectF(self.rect()), self._expression, self._blink, lift, squash,
                        self._gaze, self._tilt(), self._glow, self._look, math.sin(self._breath), sweep)
        if self._offline:
            self._paint_offline_badge(painter)
        painter.end()

    @staticmethod
    def _paint_offline_badge(p: QPainter) -> None:
        """A small crossed-out Wi-Fi badge in the top-left corner while offline mode is on."""
        r = QRectF(2, 2, 22, 22)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(BAR_EDGE), 1.0))
        p.setBrush(QColor(BAR_BG))
        p.drawEllipse(r)
        c = r.center()
        p.setPen(QPen(QColor(ICON), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for radius in (7.0, 4.2):
            p.drawArc(QRectF(c.x() - radius, c.y() - radius + 3, radius * 2, radius * 2), 45 * 16, 90 * 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(ICON))
        p.drawEllipse(QPointF(c.x(), c.y() + 3), 1.4, 1.4)
        p.setPen(QPen(QColor("#ff6b7a"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(r.left() + 5, r.bottom() - 5), QPointF(r.right() - 5, r.top() + 5))

    def enterEvent(self, event):
        self.react_curious()  # hovering counts as interaction
        super().enterEvent(event)

    # -- input ------------------------------------------------------------ #
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._drag_offset = self._press_global - self.frameGeometry().topLeft()
            self._dragging = False
            self._drag_samples = []
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            current = event.globalPosition().toPoint()
            if not self._dragging:
                if (current - self._press_global).manhattanLength() < self.DRAG_THRESHOLD:
                    return
                self._dragging = True
            now = time.monotonic()
            self._drag_samples.append((now, current))
            self._drag_samples = [(t, p) for t, p in self._drag_samples if now - t <= 0.2]
            self.move(current - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._swallow_click:
                self._swallow_click = False  # this click only dropped the pet in move mode
            elif self._dragging:
                # A fast release is a flick; a slow one just ends the drag
                if self._drag_samples and time.monotonic() - self._drag_samples[-1][0] < 0.08:
                    vx, vy = drag_velocity(self._drag_samples)
                    if math.hypot(vx, vy) >= self.FLICK_SPEED:
                        self.flicked.emit(vx, vy)
            else:
                self.react_curious()
                self.clicked.emit()  # the controller opens the conversation
        self._drag_offset = None
        self._drag_samples = []
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.ask_requested.emit()

    def contextMenuEvent(self, event):
        self.context_menu_requested.emit(event.globalPos())

    def moveEvent(self, event):
        super().moveEvent(event)
        self.moved.emit()


# --------------------------------------------------------------------------- #
# Bubbles
# --------------------------------------------------------------------------- #
class BubbleWindow(QWidget):
    """Base class for frameless, translucent, always-on-top bubbles that follow the pet."""

    def __init__(self, width: int):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(STYLE)
        self.setFixedWidth(width)
        self._anchor: QRect | None = None

        self._panel = QFrame(self)
        self._panel.setObjectName("bubble")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._panel)

        self._body = QVBoxLayout(self._panel)
        self._body.setContentsMargins(12, 10, 12, 10)
        self._body.setSpacing(6)

    def anchor_to(self, rect: QRect) -> None:
        """Remember the pet's geometry and reposition beside it. Call again whenever the pet moves."""
        self._anchor = QRect(rect)
        self._reposition()

    def show_at(self, rect: QRect) -> None:
        self.anchor_to(rect)
        self.show()
        self.raise_()
        self.activateWindow()

    def _reposition(self) -> None:
        self.adjustSize()
        if self._anchor is not None:
            place_near(self, self._anchor)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)


class InputBubble(BubbleWindow):
    """
    Single-line prompt box shown above the pet, with a microphone button for voice typing.

    It fades out once it has been quiet for a while: no typing, and the pointer is not on it.
    Any typing or hovering brings it back to full strength. Text left in the box is kept.
    """

    submitted = pyqtSignal(str)
    IDLE_FADE_MS = 6000          # quiet time before the box starts to fade
    VOICE_FADE_MS = 30000        # speaking takes longer than typing, so wait longer
    CHECK_AGAIN_MS = 2000        # while the pointer is on the box, check again this often
    HINT = "Enter to send · Esc to close · /notes /clip /script /timer /duck"
    DUCK_HINT = "Rubber duck mode: I'll ask questions, not give answers. /duck to leave."

    def __init__(self):
        super().__init__(330)
        self._edit = QLineEdit()
        self._edit.setPlaceholderText("Ask Opus something…")
        self._edit.returnPressed.connect(self._submit)
        self._edit.textEdited.connect(self._restart_idle)  # typing counts as activity
        self._mic = QPushButton("🎤")
        self._mic.setFixedSize(34, 30)
        self._mic.setToolTip("Talk instead of typing (Windows voice typing, the same as Win+H)")
        self._mic.clicked.connect(self._voice)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self._edit, 1)
        row.addWidget(self._mic)
        self._hint = QLabel(self.HINT)
        self._hint.setObjectName("hint")
        self._hint.setWordWrap(True)
        self._body.addLayout(row)
        self._body.addWidget(self._hint)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(500)
        self._fade.finished.connect(self._on_fade_done)

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._maybe_fade)

    def set_mode(self, duck: bool) -> None:
        self._edit.setPlaceholderText("Explain the problem, one step at a time…" if duck else "Ask Opus something…")
        self._hint.setText(self.DUCK_HINT if duck else self.HINT)

    def prefill(self, text: str) -> None:
        self._edit.setText(text)
        self._edit.setFocus()
        self._edit.end(False)

    def show_at(self, rect: QRect) -> None:
        self._fade.stop()
        self.setWindowOpacity(1.0)
        super().show_at(rect)
        self._edit.setFocus()
        self._restart_idle()

    def start_voice(self) -> None:
        self._voice()

    def _voice(self) -> None:
        """Hand speech-to-text to Windows: it types what you say into the box."""
        self._edit.setFocus()
        self.activateWindow()
        self._idle_timer.start(self.VOICE_FADE_MS)
        QTimer.singleShot(150, start_voice_typing)

    def _restart_idle(self, *_) -> None:
        self._idle_timer.start(self.IDLE_FADE_MS)

    def _maybe_fade(self) -> None:
        if not self.isVisible():
            return
        if self.frameGeometry().contains(QCursor.pos()):
            # The pointer is on the box, so keep it. Check again later.
            self._idle_timer.start(self.CHECK_AGAIN_MS)
            return
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _on_fade_done(self) -> None:
        self.hide()
        self.setWindowOpacity(1.0)  # ready for the next time the box is shown

    def _submit(self) -> None:
        text = self._edit.text().strip()
        if not text:
            return
        self._edit.clear()
        self._idle_timer.stop()
        self.hide()
        self.submitted.emit(text[:MAX_PROMPT_CHARS])


class ResponseCard(BubbleWindow):
    """Shows the question, the streamed answer, status, and Copy / Stop / Close controls."""

    stop_requested = pyqtSignal()
    closed = pyqtSignal()
    reply_requested = pyqtSignal()
    MAX_BODY_HEIGHT = 260

    def __init__(self):
        super().__init__(340)

        self._title = QLabel()
        self._title.setObjectName("title")
        self._close = QPushButton("×")
        self._close.setObjectName("close")
        self._close.setFixedSize(22, 22)
        self._close.clicked.connect(self.hide)

        header = QHBoxLayout()
        header.addWidget(self._title, 1)
        header.addWidget(self._close)

        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(False)
        self._browser.setFrameShape(QFrame.Shape.NoFrame)

        self._status = QLabel()
        self._status.setObjectName("hint")
        self._copy = QPushButton("Copy")
        self._copy.clicked.connect(self._copy_text)
        self._stop = QPushButton("Stop")
        self._stop.clicked.connect(self.stop_requested)
        self._reply = QPushButton("Reply")
        self._reply.clicked.connect(self.reply_requested)
        self._reply.hide()

        footer = QHBoxLayout()
        footer.addWidget(self._status, 1)
        footer.addWidget(self._copy)
        footer.addWidget(self._stop)
        footer.addWidget(self._reply)

        self._body.addLayout(header)
        self._body.addWidget(self._browser)
        self._body.addLayout(footer)

        self._text = ""
        self._dots = 0
        self._dot_timer = QTimer(self)
        self._dot_timer.timeout.connect(self._animate_dots)

        # Fade-out used after an answer has been left alone for a while; see OpusController
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(600)
        self._fade.finished.connect(self._on_fade_done)

    # -- lifecycle -------------------------------------------------------- #
    def begin(self, prompt: str) -> None:
        short = prompt if len(prompt) <= 60 else prompt[:57] + "…"
        self._title.setText(f"You: {short}")
        self._text = ""
        self._browser.clear()
        self._stop.show()
        self._reply.hide()
        self._dots = 0
        self._dot_timer.start(450)
        self._animate_dots()
        self._present()

    def append(self, delta: str) -> None:
        if self._dot_timer.isActive():
            self._dot_timer.stop()
            self._status.setText("")
        self._text += delta
        self._browser.moveCursor(QTextCursor.MoveOperation.End)
        self._browser.insertPlainText(delta)
        self._fit()

    def finish(self, status: str = "") -> None:
        self._dot_timer.stop()
        self._stop.hide()
        self._status.setText(status)
        self._fit()

    def show_message(self, title: str, text: str) -> None:
        self._dot_timer.stop()
        self._stop.hide()
        self._reply.hide()
        self._title.setText(title)
        self._status.setText("")
        self._text = text
        self._browser.setPlainText(text)
        self._present()

    # -- internals -------------------------------------------------------- #
    def _animate_dots(self) -> None:
        self._dots = self._dots % 3 + 1
        self._status.setText("Thinking" + "." * self._dots)

    def _copy_text(self) -> None:
        QApplication.clipboard().setText(self._text)
        self._status.setText("Copied")

    def _fit(self) -> None:
        """Size the text area to its content, up to MAX_BODY_HEIGHT, then reposition."""
        self._browser.document().setTextWidth(self._browser.viewport().width())
        doc_height = self._browser.document().size().height()
        height = int(min(max(doc_height + 14, 36), self.MAX_BODY_HEIGHT))
        self._browser.setFixedHeight(height)
        self._reposition()

    def fade_out(self) -> None:
        """Fade the card away. It hides itself when the fade ends."""
        if not self.isVisible():
            return
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def stop_fade(self) -> None:
        """Cancel a fade in progress and restore full opacity."""
        self._fade.stop()
        self.setWindowOpacity(1.0)

    def _on_fade_done(self) -> None:
        self.hide()
        self.setWindowOpacity(1.0)  # ready for the next time the card is shown

    def set_reply_visible(self, visible: bool) -> None:
        """The Reply button only makes sense after a finished answer."""
        self._reply.setVisible(visible)

    def present(self) -> None:
        """Bring the card back up without changing its text."""
        self._present()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.closed.emit()

    def _present(self) -> None:
        self.stop_fade()
        self._fit()
        self.show()
        self.raise_()
        # The first layout pass may not have set the text width yet. Fit once more afterwards.
        QTimer.singleShot(0, self._fit)


# --------------------------------------------------------------------------- #
# Status bubble: a small label above the pet that names the current state
# --------------------------------------------------------------------------- #
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_NEEDS_INPUT = "needs_input"
STATUS_READY = "ready"
STATUS_BLOCKED = "blocked"

# Label and dot colour for each state that shows a bubble
STATUS_LABELS = {
    STATUS_RUNNING: ("Running…", "#7aa8ff"),
    STATUS_NEEDS_INPUT: ("Needs input", "#ffd166"),
    STATUS_READY: ("Ready", "#6ee7a8"),
    STATUS_BLOCKED: ("Blocked", "#ff6b7a"),
}


def _draw_glass(p: QPainter, rect: QRectF, radius: float) -> None:
    """Apple-style glass: a soft shadow, a clear light body, a bright rim, and a specular highlight."""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(0, 0, 0, 55))
    p.drawRoundedRect(rect.translated(0, 3), radius, radius)  # shadow beneath the glass
    body = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    body.setColorAt(0.0, QColor(58, 62, 80, 200))
    body.setColorAt(0.5, QColor(26, 29, 42, 190))
    body.setColorAt(1.0, QColor(14, 16, 24, 200))
    p.setBrush(QBrush(body))
    rim = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    rim.setColorAt(0.0, QColor(255, 255, 255, 170))
    rim.setColorAt(1.0, QColor(255, 255, 255, 40))
    p.setPen(QPen(QBrush(rim), 1.2))
    p.drawRoundedRect(rect, radius, radius)
    gleam = rect.adjusted(4, 2.5, -4, -rect.height() * 0.55)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 34))
    p.drawRoundedRect(gleam, min(radius, gleam.height() / 2), min(radius, gleam.height() / 2))
    # Highlights at the top corners, where the light catches the edge
    p.setBrush(QColor(255, 255, 255, 150))
    for corner in (rect.left() + radius * 0.45, rect.right() - radius * 0.45):
        p.drawEllipse(QPointF(corner, rect.top() + radius * 0.45), 2.0, 2.0)


class StatusBubble(QWidget):
    """Pill above the pet with a coloured dot and a short state label. Clicking it opens the thread."""

    clicked = pyqtSignal()
    HEIGHT = 26

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Open the conversation")
        self._font = QFont("Segoe UI", 9)
        self._font.setWeight(QFont.Weight.DemiBold)
        self._text = ""
        self._color = QColor(ICON)
        self._anchor: QRect | None = None

    def set_state(self, label: str, color: str) -> None:
        self._text = label
        self._color = QColor(color)
        width = QFontMetrics(self._font).horizontalAdvance(label) + 36
        self.setFixedSize(width, self.HEIGHT)
        self._reposition()
        self.update()

    def anchor_above(self, rect: QRect) -> None:
        """Follow the pet and sit just above its head."""
        self._anchor = QRect(rect)
        self._reposition()

    def _reposition(self) -> None:
        if self._anchor is None:
            return
        area = (QApplication.screenAt(self._anchor.center()) or QApplication.primaryScreen()).availableGeometry()
        x = self._anchor.center().x() - self.width() // 2
        y = self._anchor.top() - self.height() - 2
        x = max(area.left() + 4, min(x, area.right() - self.width() - 4))
        y = max(area.top() + 4, y)
        self.move(x, y)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        _draw_glass(p, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self.HEIGHT / 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(QRectF(12, self.HEIGHT / 2 - 4, 8, 8))
        p.setFont(self._font)
        p.setPen(QColor(ICON))
        p.drawText(QRectF(26, 0, self.width() - 30, self.HEIGHT),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._text)
        p.end()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


# --------------------------------------------------------------------------- #
# Action bar: the dark pill under the pet (edit, waveform, chevron)
# --------------------------------------------------------------------------- #
BUTTON_D = 24                    # diameter of each round button
BAR_SPACING = 8                  # gap between buttons; the divider sits in it
BAR_PAD_X = 6
BAR_PAD_Y = 5
BAR_W = BUTTON_D * 3 + BAR_SPACING * 2 + BAR_PAD_X * 2
BAR_H = BUTTON_D + BAR_PAD_Y * 2
BAR_BG = "#262a3a"
BAR_EDGE = "#3d4466"
DIVIDER = "#4a5172"
BUTTON_HOVER = "#363c55"
BUTTON_PRESSED = "#4a5275"
ICON = "#e9edff"


def _at(r: QRectF, fx: float, fy: float) -> QPointF:
    """Point at fractional position (fx, fy) inside rect r, so icons scale with their button."""
    return QPointF(r.left() + fx * r.width(), r.top() + fy * r.height())


def _icon_pen(width: float) -> QPen:
    return QPen(QColor(ICON), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin)


def _paint_edit(p: QPainter, r: QRectF) -> None:
    """Rounded square with a pencil: the 'Ask' icon."""
    p.setPen(_icon_pen(1.8))
    p.setBrush(Qt.BrushStyle.NoBrush)
    square = QPainterPath()
    square.addRoundedRect(QRectF(_at(r, 0.10, 0.20), _at(r, 0.80, 0.90)),
                          r.width() * 0.16, r.width() * 0.16)
    p.drawPath(square)
    p.setPen(_icon_pen(2.2))
    p.drawLine(_at(r, 0.46, 0.56), _at(r, 0.90, 0.12))


def _paint_waveform(p: QPainter, r: QRectF) -> None:
    """Five audio bars: the 'Show last answer' icon."""
    p.setPen(_icon_pen(2.0))
    for fx, fh in ((0.16, 0.28), (0.33, 0.56), (0.50, 0.86), (0.67, 0.50), (0.84, 0.24)):
        p.drawLine(_at(r, fx, 0.5 - fh / 2), _at(r, fx, 0.5 + fh / 2))


def _paint_chevron(p: QPainter, r: QRectF) -> None:
    """Downward chevron: opens the menu."""
    p.setPen(_icon_pen(2.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath(_at(r, 0.24, 0.38))
    path.lineTo(_at(r, 0.50, 0.64))
    path.lineTo(_at(r, 0.76, 0.38))
    p.drawPath(path)


def _paint_grid(p: QPainter, r: QRectF) -> None:
    """Four small squares: the Dashboard icon."""
    p.setPen(_icon_pen(1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    for fx, fy in ((0.12, 0.12), (0.54, 0.12), (0.12, 0.54), (0.54, 0.54)):
        p.drawRoundedRect(QRectF(_at(r, fx, fy), _at(r, fx + 0.34, fy + 0.34)), 1.5, 1.5)


class IconButton(QAbstractButton):
    """Round, flat button: a soft disc on hover or press, plus its icon."""

    def __init__(self, tooltip: str, paint_icon, parent: QWidget | None = None):
        super().__init__(parent)
        self._paint_icon = paint_icon
        self.setToolTip(tooltip)
        self.setFixedSize(BUTTON_D, BUTTON_D)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isDown() or self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            disc = QRectF(self.rect()).adjusted(1, 1, -1, -1)
            sheen = QLinearGradient(disc.topLeft(), disc.bottomLeft())
            sheen.setColorAt(0.0, QColor(255, 255, 255, 170))
            sheen.setColorAt(1.0, QColor(255, 255, 255, 30))
            p.setBrush(QBrush(sheen))
            p.drawEllipse(disc)
        self._paint_icon(p, QRectF(self.rect()).adjusted(6, 6, -6, -6))
        p.end()


class ActionBar(QWidget):
    """Dark pill with three round buttons, shown just under the pet and following it."""

    ask_clicked = pyqtSignal()
    last_answer_clicked = pyqtSignal()
    menu_clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(BAR_W, BAR_H)
        self._anchor: QRect | None = None

        self._ask = IconButton("Ask Opus", _paint_edit)
        self._last = IconButton("Dashboard", _paint_grid)
        self._more = IconButton("Menu", _paint_chevron)
        self._buttons = (self._ask, self._last, self._more)
        self._ask.clicked.connect(self.ask_clicked)
        self._last.clicked.connect(self.last_answer_clicked)
        self._more.clicked.connect(self.menu_clicked)

        row = QHBoxLayout(self)
        row.setContentsMargins(BAR_PAD_X, BAR_PAD_Y, BAR_PAD_X, BAR_PAD_Y)
        row.setSpacing(BAR_SPACING)
        for button in self._buttons:
            row.addWidget(button)

        # Hidden until the pointer is over the pet or the pill; see OpusController._poll_hover
        self.setWindowOpacity(0.0)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(180)

    def fade_to(self, opacity: float) -> None:
        """Fade the whole pill in (1.0) or out (0.0)."""
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(opacity)
        self._fade.start()

    def menu_anchor(self) -> QPoint:
        """Global point just under the chevron, where the menu opens."""
        return self._more.mapToGlobal(QPoint(0, self._more.height()))

    def anchor_below(self, rect: QRect) -> None:
        """Follow the pet. Sits just under its body and is kept on screen."""
        self._anchor = QRect(rect)
        self._reposition()

    def _reposition(self) -> None:
        if self._anchor is None:
            return
        area = QApplication.screenAt(self._anchor.center()) or QApplication.primaryScreen()
        bounds = area.availableGeometry()
        x = self._anchor.center().x() - self.width() // 2
        y = self._anchor.bottom() - 4
        x = max(bounds.left() + 4, min(x, bounds.right() - self.width() - 4))
        y = max(bounds.top() + 4, min(y, bounds.bottom() - self.height() - 4))
        self.move(x, y)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        _draw_glass(p, rect, BAR_H / 2)
        p.setPen(QPen(QColor(DIVIDER), 1.0))
        for left, right in zip(self._buttons, self._buttons[1:]):
            x = (left.geometry().right() + right.geometry().left() + 1) / 2
            p.drawLine(QPointF(x, BAR_PAD_Y + 7), QPointF(x, BAR_H - BAR_PAD_Y - 7))
        p.end()


# --------------------------------------------------------------------------- #
# Speech bubble, dashboard, script review, and background helpers
# --------------------------------------------------------------------------- #
class SayBubble(BubbleWindow):
    """
    A short speech bubble for things Opus notices on its own: code review comments, break
    nudges, finished downloads and builds, timers. It fades away by itself; a click dismisses it.
    """

    def __init__(self):
        super().__init__(270)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        self._body.addWidget(self._label)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(500)
        self._fade.finished.connect(self._on_fade_done)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.fade_out)

    def say(self, text: str, seconds: float | None = None) -> None:
        self._label.setText(text)
        self._fade.stop()
        self.setWindowOpacity(1.0)
        self._reposition()
        self.show()
        self.raise_()
        self._timer.start(int((seconds or min(14.0, 5.0 + len(text) / 18)) * 1000))

    def fade_out(self) -> None:
        if self.frameGeometry().contains(QCursor.pos()):
            self._timer.start(1500)  # being read; wait
            return
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _on_fade_done(self) -> None:
        self.hide()
        self.setWindowOpacity(1.0)

    def mouseReleaseEvent(self, event):
        self._timer.stop()
        self.hide()
        super().mouseReleaseEvent(event)


class DashboardPanel(QWidget):
    """
    A small panel pinned beside the pet, for a glance at the machine and the day: CPU, memory,
    battery, the next calendar event, focus time, the pet's needs and the last thing Opus said.
    """

    WIDTH = 250
    ROWS = (("cpu", "CPU"), ("memory", "Memory"), ("battery", "Battery"), ("next", "Next"),
            ("focus", "Focus"), ("needs", "Needs"))

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet(STYLE)
        self.setFixedWidth(self.WIDTH)
        self._anchor: QRect | None = None

        panel = QFrame(self)
        panel.setObjectName("bubble")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Opus")
        title.setObjectName("title")
        self._mode = QLabel()
        self._mode.setObjectName("hint")
        header.addWidget(title, 1)
        header.addWidget(self._mode)
        layout.addLayout(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        self._values: dict[str, QLabel] = {}
        for row, (key, label) in enumerate(self.ROWS):
            name = QLabel(label)
            name.setObjectName("hint")
            value = QLabel("…")
            value.setWordWrap(True)
            grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value, row, 1)
            self._values[key] = value
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        self._said = QLabel()
        self._said.setObjectName("hint")
        self._said.setWordWrap(True)
        layout.addWidget(self._said)

    def update_values(self, values: dict) -> None:
        for key, label in self._values.items():
            label.setText(values.get(key, "—"))
        said = values.get("said") or ""
        self._said.setText(f"Last said: “{said[:140]}{'…' if len(said) > 140 else ''}”" if said else "")
        self._said.setVisible(bool(said))
        self._mode.setText("✈ offline" if values.get("offline") else "online")
        self.adjustSize()
        self._reposition()

    def anchor_beside(self, rect: QRect) -> None:
        self._anchor = QRect(rect)
        self._reposition()

    def _reposition(self) -> None:
        if self._anchor is None:
            return
        area = (QApplication.screenAt(self._anchor.center()) or QApplication.primaryScreen()).availableGeometry()
        x = self._anchor.left() - self.width() - 8
        if x < area.left() + 4:
            x = self._anchor.right() + 8  # no room on the left, so pin it on the right
        y = self._anchor.bottom() - self.height()
        x = max(area.left() + 4, min(x, area.right() - self.width() - 4))
        y = max(area.top() + 4, min(y, area.bottom() - self.height() - 4))
        self.move(x, y)


class ScriptDialog(QDialog):
    """A script the model wrote, and the folder it would run in. Nothing runs until you press Run."""

    run_requested = pyqtSignal()

    def __init__(self, task: str, folder: str, code: str):
        super().__init__()
        self.task, self.folder, self.code = task, folder, code
        self.setWindowTitle("Opus wrote a script: read it before running")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(700, 560)

        intro = QLabel(f"<b>Task:</b> {escape(task)}<br><b>Runs in:</b> {escape(folder)}<br>"
                       "Nothing runs until you press <b>Run</b>. Read the script first.")
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        mono = QFont("Consolas", 10)
        self._code = QPlainTextEdit(code)
        self._code.setReadOnly(True)
        self._code.setFont(mono)
        self._code.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setFont(mono)
        self._output.setPlaceholderText("The script's output appears here after it runs.")
        self._output.setMaximumHeight(170)

        self._status = QLabel()
        self._copy = QPushButton("Copy script")
        self._copy.clicked.connect(lambda: QApplication.clipboard().setText(self.code))
        self._run = QPushButton("Run")
        self._run.clicked.connect(self._on_run)
        self._close = QPushButton("Cancel")
        self._close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self._status, 1)
        buttons.addWidget(self._copy)
        buttons.addWidget(self._run)
        buttons.addWidget(self._close)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self._code, 1)
        layout.addWidget(self._output)
        layout.addLayout(buttons)

    def _on_run(self) -> None:
        self._run.setEnabled(False)
        self._status.setText("Running…")
        self.run_requested.emit()

    def show_result(self, result: dict) -> None:
        seconds = result.get("seconds", 0)
        if result.get("timed_out"):
            self._status.setText(f"Stopped after {seconds:g}s: it took too long.")
        elif result.get("exit_code") == 0:
            self._status.setText(f"Done in {seconds:g}s.")
        else:
            self._status.setText(f"Failed with exit code {result.get('exit_code')}.")
        self._output.setPlainText(result.get("output") or "(no output)")
        self._close.setText("Close")


class BackgroundJob(QObject):
    """Runs a plain function on a daemon thread, then emits finished(result) or failed(message)."""

    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, *args, parent: QObject | None = None):
        super().__init__(parent)
        self._fn = fn
        self._args = args

    def start(self) -> None:
        threading.Thread(target=self._run, name="opus-job", daemon=True).start()

    def _run(self) -> None:
        try:
            result = self._fn(*self._args)
        except Exception as exc:  # noqa: BLE001 - report every failure back to the GUI
            log.warning("Background job failed: %s", exc)
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


class ToolBridge(QObject):
    """Lets tool handlers, which run on a worker thread, ask the GUI thread to do something."""

    timer_requested = pyqtSignal(float, str)


# --------------------------------------------------------------------------- #
# Global hotkey (Win32 RegisterHotKey)
# --------------------------------------------------------------------------- #
if sys.platform == "win32":
    USER32 = ctypes.WinDLL("user32", use_last_error=True)
    USER32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    USER32.RegisterHotKey.restype = wintypes.BOOL
    USER32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    USER32.UnregisterHotKey.restype = wintypes.BOOL

_MODIFIER_FLAGS = {
    "alt": 0x0001,
    "ctrl": 0x0002, "control": 0x0002,
    "shift": 0x0004,
    "win": 0x0008, "windows": 0x0008,
}
_SPECIAL_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}


def _key_to_vk(name: str) -> int:
    if len(name) == 1 and (name.isalpha() or name.isdigit()):
        return ord(name.upper())
    if name in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[name]
    if re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", name):
        return 0x70 + int(name[1:]) - 1  # VK_F1 == 0x70
    raise ValueError(f"unknown key {name!r}")


def parse_hotkey(combo: str) -> tuple[int, int]:
    """Turn 'ctrl+alt+space' into (modifier_flags, virtual_key). Requires at least one modifier."""
    modifiers = 0
    key = None
    for raw in combo.split("+"):
        part = raw.strip().lower()
        if not part:
            continue
        if part in _MODIFIER_FLAGS:
            modifiers |= _MODIFIER_FLAGS[part]
        elif key is None:
            key = _key_to_vk(part)
        else:
            raise ValueError(f"hotkey {combo!r} has more than one non-modifier key")
    if key is None:
        raise ValueError(f"hotkey {combo!r} has no key")
    if modifiers == 0:
        raise ValueError(f"hotkey {combo!r} needs at least one modifier (ctrl, alt, shift or win)")
    return modifiers, key


class GlobalHotkey(QAbstractNativeEventFilter):
    """
    One system-wide hotkey.

    RegisterHotKey is tied to a window handle. We use a hidden QWidget that the
    controller owns, so WM_HOTKEY arrives on a Qt-managed window and passes
    through this native event filter on the GUI thread.
    """

    HOTKEY_ID = 0xB001
    WM_HOTKEY = 0x0312
    MOD_NOREPEAT = 0x4000

    def __init__(self, host: QWidget, combo: str, callback, hotkey_id: int = 0xB001):
        super().__init__()
        self.HOTKEY_ID = hotkey_id  # each registered hotkey needs its own id
        self._host = host
        self._callback = callback
        self._combo = combo
        self._modifiers, self._vk = parse_hotkey(combo)  # raises ValueError if malformed
        self._hwnd: int | None = None

    def register(self) -> bool:
        if sys.platform != "win32":
            log.warning("Global hotkeys are only supported on Windows.")
            return False
        hwnd = int(self._host.winId())
        ok = USER32.RegisterHotKey(hwnd, self.HOTKEY_ID, self._modifiers | self.MOD_NOREPEAT, self._vk)
        if not ok:
            err = ctypes.get_last_error()
            # 1409 == ERROR_HOTKEY_ALREADY_REGISTERED: another app owns this combination.
            log.error("RegisterHotKey(%s) failed: %s", self._combo, ctypes.WinError(err))
            return False
        self._hwnd = hwnd
        QApplication.instance().installNativeEventFilter(self)
        log.info("Global hotkey registered: %s", self._combo)
        return True

    def unregister(self) -> None:
        if self._hwnd is None:
            return
        QApplication.instance().removeNativeEventFilter(self)
        USER32.UnregisterHotKey(self._hwnd, self.HOTKEY_ID)
        self._hwnd = None

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) != b"windows_generic_MSG":
            return False, 0
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == self.WM_HOTKEY and msg.wParam == self.HOTKEY_ID and msg.hWnd == self._hwnd:
            try:
                self._callback()
            except Exception:  # noqa: BLE001 - never let a UI error kill the event loop
                log.exception("Hotkey callback failed")
            return True, 0
        return False, 0


# --------------------------------------------------------------------------- #
CHATTER_PROMPT = (
    "You are Opus, a small desktop companion. The user is working right now. You can see only the app "
    "in front and its window title. Make at most one short comment (under 20 words) that is genuinely useful "
    "for what they are doing: a brainstorm idea for code or writing, a question worth considering, or a tip "
    "for the app. Do not repeat yourself, do not lecture, do not mention the window title unless it helps. "
    "If you have nothing useful to add, reply with exactly: NONE"
)

HELP_TEXT = """Ask: double-click me, press Ctrl+Alt+Space, or click the pencil.
Move: Ctrl+Alt+M, move the mouse, click to drop. Flick down to hide me.
Rubber duck: Ctrl+Alt+D. I ask questions instead of answering.

Commands (type them in the box):
/notes <question>   answer from your own files
/clip <words>       search what you copied
/script <task>      write a script (runs only after you approve)
/timer 10 tea       a timer
/duck               rubber duck mode on or off

Calendar: menu > Connect a calendar (.ics). Paste the path of an .ics file,
or the secret iCal address from Google Calendar (Settings > your calendar >
Secret address in iCal format) or Outlook (Calendar > Shared calendars > ICS).
Only single events show; repeating events are not expanded.
I remind you 10 minutes before the next one.
"""


# --------------------------------------------------------------------------- #
# Controller: wires the pet, bubbles, tray, hotkeys, agent workers and companion services together
# --------------------------------------------------------------------------- #
class OpusController(QObject):
    HEALTH_INTERVAL_MS = 5 * 60 * 1000
    CARD_FADE_DELAY_MS = 8000       # how long a finished answer stays on screen before it fades
    CARD_HOVER_RECHECK_MS = 2000    # while the pointer is on the card, check again this often
    STATS_INTERVAL_MS = 2000        # CPU, battery and the dashboard
    HABIT_INTERVAL_MS = 10_000      # foreground app, breaks and the pet's needs
    WATCH_INTERVAL_MS = 1000        # saved code, finished downloads and opus_run events
    CALENDAR_INTERVAL_MS = 15 * 60 * 1000
    SAVE_EVERY_HABIT_TICKS = 30     # persist habits and needs about every five minutes
    MOVE_TIMEOUT_S = 8.0            # move mode ends by itself after this long
    MAX_REVIEW_BYTES = 200_000      # larger files are not sent for review
    LOW_BATTERY = 15                # percent, on battery power, that makes the pet worried

    def __init__(self, settings):
        super().__init__()
        self._settings = settings
        self._config = Config()
        network.set_offline(bool(self._config.get("offline", False)))

        # Chat state
        self._conversation = Conversation(settings.max_turns)
        self._duck_conversation = Conversation(settings.max_turns)
        self._request: AskRequest | None = None
        self._status = STATUS_IDLE
        self._duck_mode = False
        self._last_prompt = ""
        self._last_reply = ""
        self._pending_question = ""
        self._last_said = ""
        self._pending_sources: list[str] = []
        self._health_ok = True
        self._health_warned = False
        self._battery_warned = False

        # Companion services (companion.py). Everything they record stays on this machine.
        self._stats = SystemStats()
        self._stats.sample()
        self._habits = HabitTracker()
        self._needs = PetNeeds(self._config.get("needs"))
        self._clipboard = ClipboardStore()
        self._knowledge = KnowledgeBase()
        self._inbox = EventInbox()
        self._personalities = load_personalities()
        self._code_watcher: FolderWatcher | None = None
        self._download_watcher: FolderWatcher | None = None
        self._file_cache: dict[Path, str] = {}     # last reviewed text of each watched file, for diffs
        self._review_queue: list[Path] = []
        self._review_task: ModelTask | None = None
        self._jobs: set[QObject] = set()            # background workers, kept alive until they report back
        self._timers: list[QTimer] = []
        self._calendar_event = None
        self._reminded: set[str] = set()
        self._script_dialog: ScriptDialog | None = None
        self._indexing = False
        self._habit_ticks = 0
        self._last_habit_tick = time.monotonic()
        self._moving = False
        self._move_started = 0.0
        self._move_origin = QPoint()
        self._move_samples: list[tuple[float, QPoint]] = []
        self._glide: QPropertyAnimation | None = None
        self._pill_shown = False

        # Windows
        self.pet = PetWidget()
        self.input = InputBubble()
        self.card = ResponseCard()
        self.bar = ActionBar()
        self.status_bubble = StatusBubble()
        self.say_bubble = SayBubble()
        self.dashboard = DashboardPanel()

        # Tools the model may call. Handlers run on the request's worker thread.
        self._bridge = ToolBridge()
        self._bridge.timer_requested.connect(self.start_timer)
        self._toolbox = self._build_toolbox()

        # Menu, shared by the pet, the chevron button and the tray icon
        self._menu = QMenu()
        self._build_menu()
        self._menu.aboutToShow.connect(self._sync_menu)

        # Tray icon
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("Opus: starting…")
        self.tray.setContextMenu(self._menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        # Global hotkeys, on a hidden host window
        self._host = QWidget()
        self._hotkeys: list[GlobalHotkey] = []
        self._register_hotkey(settings.hotkey, self.toggle_input, "STACKY_HOTKEY", 0xB001)
        self._register_hotkey(os.environ.get("STACKY_MOVE_HOTKEY", "ctrl+alt+m"), self.start_move_mode,
                              "STACKY_MOVE_HOTKEY", 0xB002)
        self._register_hotkey(os.environ.get("STACKY_DUCK_HOTKEY", "ctrl+alt+d"), self.toggle_duck_mode,
                              "STACKY_DUCK_HOTKEY", 0xB003)

        # Timers
        self._save_pos_timer = QTimer(self)
        self._save_pos_timer.setSingleShot(True)
        self._save_pos_timer.timeout.connect(self._save_position)
        self._card_fade_timer = QTimer(self)
        self._card_fade_timer.setSingleShot(True)
        self._card_fade_timer.timeout.connect(self._maybe_fade_card)
        self._move_timer = QTimer(self)
        self._move_timer.timeout.connect(self._move_tick)
        self._every(self.HEALTH_INTERVAL_MS, self.check_health)
        self._every(120, self._poll_hover)
        self._every(self.STATS_INTERVAL_MS, self._on_stats_tick)
        self._every(self.HABIT_INTERVAL_MS, self._on_habit_tick)
        self._every(self.WATCH_INTERVAL_MS, self._on_watch_tick)
        self._every(self.CALENDAR_INTERVAL_MS, self._refresh_calendar)
        self._every(60_000, self._check_calendar_reminder)
        self._chatty_timer = QTimer(self)
        self._chatty_timer.setSingleShot(True)
        self._chatty_timer.timeout.connect(self._chatter)
        self._schedule_chatter()

        # Signals. Each handler is a bound method on a QObject living on the GUI thread.
        self.pet.moved.connect(self._follow_pet)
        self.pet.ask_requested.connect(self.open_input)
        self.pet.context_menu_requested.connect(self._menu.popup)
        self.pet.clicked.connect(self._on_pet_clicked)
        self.pet.flicked.connect(self._on_flick)
        self.input.submitted.connect(self.ask)
        self.card.stop_requested.connect(self.cancel)
        self.card.closed.connect(self._refresh_status_bubble)
        self.card.reply_requested.connect(self.reply)
        self.bar.ask_clicked.connect(self.open_input)
        self.bar.last_answer_clicked.connect(self.toggle_dashboard)
        self.bar.menu_clicked.connect(self._show_bar_menu)
        self.status_bubble.clicked.connect(self.open_thread)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard)

        # Put the pet back where it was last time
        self._restore_position()
        self.pet.set_offline_badge(network.offline)
        self.pet.show()
        self._follow_pet()
        self.bar.show()

        # Start whatever was switched on last time
        folder = self._config.get("watch_folder")
        if folder:
            self._start_code_watcher(folder, announce=False)
        if self._config.get("download_alerts", True):
            self._start_download_watcher()
        if self._config.get("dashboard", False):
            self._set_dashboard(True)
        self._run_job(self._knowledge.load, self._on_knowledge_loaded)
        self._refresh_calendar()
        self.check_health()
        self.set_status(STATUS_IDLE)
        log.info("Opus ready: model=%s url=%s offline=%s", settings.model, settings.ollama_url, network.offline)

    # -- small helpers --------------------------------------------------------- #
    def notify(self, title: str, text: str) -> None:
        self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 5000)

    def _every(self, ms: int, slot) -> QTimer:
        timer = QTimer(self)
        timer.timeout.connect(slot)
        timer.start(ms)
        return timer

    def _register_hotkey(self, combo: str, callback, env_name: str, hotkey_id: int) -> None:
        try:
            hotkey = GlobalHotkey(self._host, combo, callback, hotkey_id)
        except ValueError as exc:
            log.error("Invalid %s: %s", env_name, exc)
            self.notify("Hotkey not set", f"{env_name}: {exc}")
            return
        if hotkey.register():
            self._hotkeys.append(hotkey)
        else:
            self.notify("Hotkey unavailable", f"{combo} is already in use. Set {env_name} to something else.")

    def say(self, text: str, look: str | None = None, sound: str | None = None) -> None:
        """Opus says something on its own, in the speech bubble (or the tray while the pet is hidden)."""
        self._last_said = text
        if look:
            self.pet.react(look, 4000)
        if sound and self._config.get("sounds", True):
            play_sound(sound)
        if self.pet.isVisible():
            self.say_bubble.say(text)
        else:
            self.notify("Opus", text)
        self._refresh_dashboard()

    # -- background work -------------------------------------------------------- #
    def _run_job(self, fn, on_done, *args) -> None:
        """Run fn(*args) on a worker thread; on_done(result) runs back on the GUI thread."""
        job = BackgroundJob(fn, *args, parent=self)
        job.finished.connect(on_done)
        job.failed.connect(self._on_job_failed)
        job.finished.connect(self._forget_job)
        job.failed.connect(self._forget_job)
        self._jobs.add(job)
        job.start()

    def _start_task(self, task: ModelTask, on_done, on_failed=None) -> None:
        """Start a one-shot model task; on_done(text) runs back on the GUI thread."""
        task.done.connect(on_done)
        task.failed.connect(on_failed or self._on_task_failed)
        task.done.connect(self._forget_job)
        task.failed.connect(self._forget_job)
        self._jobs.add(task)
        task.start()

    def _forget_job(self, *_) -> None:
        job = self.sender()
        if job in self._jobs:
            self._jobs.discard(job)
            job.deleteLater()

    def _on_job_failed(self, message: str) -> None:
        log.warning("Background job failed: %s", message)

    def _on_task_failed(self, message: str) -> None:
        task = self.sender()
        if getattr(task, "user_facing", False):
            self.say(f"Sorry, that didn't work: {message}", look="worried")
        else:
            log.info("Model task failed: %s", message)

    # -- windows, position and the tray ------------------------------------------- #
    def _follow_pet(self) -> None:
        rect = self.pet.frameGeometry()
        self.input.anchor_to(rect)
        self.card.anchor_to(rect)
        self.bar.anchor_below(rect)
        self.status_bubble.anchor_above(rect)
        self.say_bubble.anchor_to(rect.adjusted(0, -30, 0, 0))  # above the status bubble
        self.dashboard.anchor_beside(rect)
        self._save_pos_timer.start(800)

    def _restore_position(self) -> None:
        saved = self._config.get("pet_pos")
        if isinstance(saved, list) and len(saved) == 2:
            point = QPoint(int(saved[0]), int(saved[1]))
            if QApplication.screenAt(point + QPoint(PET_W // 2, PET_H // 2)) is not None:
                self.pet.move(point)
                return
        area = QApplication.primaryScreen().availableGeometry()
        self.pet.move(area.right() - PET_W - 40, area.bottom() - PET_H - 40)

    def _save_position(self) -> None:
        self._config.set("pet_pos", [self.pet.x(), self.pet.y()])

    # -- commentary on what you are doing -------------------------------------- #
    def _schedule_chatter(self, minutes: float | None = None) -> None:
        if not self._config.get("chatty", True):
            return
        wait = minutes if minutes is not None else random.uniform(8, 20)
        self._chatty_timer.start(int(wait * 60_000))

    def _toggle_chatty(self, *_) -> None:
        enabled = not self._config.get("chatty", True)
        self._config.set("chatty", enabled)
        if enabled:
            self._schedule_chatter(1)
            self.say("I'll comment now and then, when you're busy.", look="curious")
        else:
            self._chatty_timer.stop()
            self.say("Quiet now. I won't comment unless you ask.")

    def _chatter(self) -> None:
        """Every so often, look at the app in front and offer one useful comment. Stays quiet when busy."""
        if not self._config.get("chatty", True):
            return
        if (self._request is not None or self._status != STATUS_IDLE or not self.pet.isVisible()
                or self._stats.idle_seconds() > 120 or self._review_task is not None):
            self._schedule_chatter(2)  # try again soon, not at the normal interval
            return
        app, title, pid = self._stats.foreground_app()
        if not app or pid == os.getpid():
            self._schedule_chatter(2)
            return
        clock = datetime.now().strftime("%H:%M")
        user = (f"App in front: {app}" + "\n" + f"Window title: {title[:120]}" + "\n" + f"Time: {clock}")
        task = ModelTask(self._settings, CHATTER_PROMPT, user, parent=self, temperature=0.8)
        self._start_task(task, self._on_chatter, self._on_chatter_failed)

    def _on_chatter(self, reply: str) -> None:
        line = reply.strip().splitlines()[0].strip() if reply.strip() else ""
        if line and line.upper() != "NONE" and not line.upper().startswith("NONE"):
            self.say(line[:160], look="curious")
        self._schedule_chatter()

    def _on_chatter_failed(self, message: str) -> None:
        log.info("Commentary skipped: %s", message)
        self._schedule_chatter()

    def _poll_hover(self) -> None:
        """Fade the action pill in while the pointer is over the pet or the pill, and out otherwise."""
        cursor = QCursor.pos()
        inside = self.pet.isVisible() and (
            self.pet.frameGeometry().contains(cursor)
            or (self.bar.isVisible() and self.bar.frameGeometry().contains(cursor))
        )
        if inside != self._pill_shown:
            self._pill_shown = inside
            self.bar.fade_to(1.0 if inside else 0.0)

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_input()
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_pet()

    def toggle_pet(self) -> None:
        if self.pet.isVisible():
            for window in (self.pet, self.bar, self.input, self.card, self.say_bubble, self.dashboard):
                window.hide()
        else:
            self.pet.show()
            self.bar.show()
            if self._config.get("dashboard", False):
                self._set_dashboard(True)
        self._refresh_status_bubble()

    def _show_bar_menu(self) -> None:
        self._menu.popup(self.bar.menu_anchor())

    def open_input(self, prefill: str | None = None) -> None:
        self.pet.show()
        self.bar.show()
        self.input.show_at(self.pet.frameGeometry())
        if isinstance(prefill, str):
            self.input.prefill(prefill)

    def voice_mode(self) -> None:
        """The middle pill button: open the prompt box and start Windows voice typing in it."""
        self.open_input()
        self.input.start_voice()

    def toggle_input(self) -> None:
        """Hotkey handler: open the prompt box, or close it if it is already open."""
        if self.input.isVisible():
            self.input.hide()
        else:
            self.open_input()

    # -- menu --------------------------------------------------------------------- #
    def _build_menu(self) -> None:
        m = self._menu
        m.addAction("Ask Opus…", lambda: self.open_input())
        m.addAction("Help and commands", self.show_help)
        self._cancel_action = m.addAction("Stop answering", self.cancel)
        m.addAction("Show last answer", self.show_last)
        m.addSeparator()

        self._duck_action = m.addAction("Rubber duck mode", self.toggle_duck_mode)
        self._duck_action.setCheckable(True)
        self._personality_menu = m.addMenu("Personality")
        self._personality_group: QActionGroup | None = None
        self._rebuild_personality_menu()
        m.addSeparator()

        self._watch_action = m.addAction("Watch a code folder…", self._on_watch_action)
        notes = m.addMenu("My notes")
        notes.addAction("Ask my notes…", lambda: self.open_input(prefill="/notes "))
        notes.addAction("Add a notes folder…", self.add_notes_folder)
        notes.addAction("Rebuild the index", self.rebuild_notes)
        self._forget_notes_action = notes.addAction("Forget all notes folders", self.forget_notes_folders)
        clip = m.addMenu("Clipboard")
        clip.addAction("Recent clips", self.show_recent_clips)
        clip.addAction("Group by topic", self.group_clips)
        self._clip_history_action = clip.addAction("Keep clipboard history", self._toggle_clipboard_history)
        self._clip_history_action.setCheckable(True)
        clip.addAction("Clear clipboard history…", self.clear_clips)
        m.addAction("Write a script…", lambda: self.open_input(prefill="/script "))
        m.addAction("Set a timer…", self.ask_timer)
        m.addAction("Feed Opus", self.feed)
        self._chatty_action = m.addAction("Chatty mode (comment on what I'm doing)", self._toggle_chatty)
        self._chatty_action.setCheckable(True)
        m.addSeparator()

        self._dashboard_action = m.addAction("Dashboard", self.toggle_dashboard)
        self._dashboard_action.setCheckable(True)
        m.addAction("Connect a calendar (.ics)…", self.choose_calendar)
        self._downloads_action = m.addAction("Download alerts", self._toggle_download_alerts)
        self._downloads_action.setCheckable(True)
        self._sounds_action = m.addAction("Sounds", self._toggle_sounds)
        self._sounds_action.setCheckable(True)
        self._offline_action = m.addAction("Offline mode", self._toggle_offline)
        self._offline_action.setCheckable(True)
        m.addSeparator()

        m.addAction("Check Ollama", lambda: self.check_health(announce=True))
        m.addAction("Clear conversation", self.clear_history)
        m.addAction("Hide / show pet", self.toggle_pet)
        m.addAction("Quit Opus", QApplication.instance().quit)

    def _sync_menu(self) -> None:
        """Bring checkmarks and labels up to date just before the menu opens."""
        self._cancel_action.setEnabled(self._request is not None)
        self._duck_action.setChecked(self._duck_mode)
        watch = self._config.get("watch_folder")
        self._watch_action.setText(f"Stop watching {Path(watch).name}" if watch else "Watch a code folder…")
        self._forget_notes_action.setEnabled(bool(self._config.get("notes_folders")))
        self._clip_history_action.setChecked(bool(self._config.get("clipboard_history", True)))
        self._dashboard_action.setChecked(self.dashboard.isVisible())
        self._downloads_action.setChecked(self._download_watcher is not None)
        self._sounds_action.setChecked(bool(self._config.get("sounds", True)))
        self._offline_action.setChecked(network.offline)
        self._chatty_action.setChecked(bool(self._config.get('chatty', True)))

    def _rebuild_personality_menu(self) -> None:
        menu = self._personality_menu
        menu.clear()
        if self._personality_group is not None:
            self._personality_group.deleteLater()
        group = QActionGroup(menu)
        group.setExclusive(True)
        current = self._current_personality().key
        for persona in self._personalities:
            if persona.key == "rubber-duck":
                continue  # the duck has its own mode
            action = menu.addAction(persona.name)
            action.setCheckable(True)
            action.setChecked(persona.key == current)
            action.setData(persona.key)
            group.addAction(action)
        group.triggered.connect(self._on_personality_chosen)
        self._personality_group = group
        menu.addSeparator()
        menu.addAction("Open personalities folder", self._open_personalities_folder)
        menu.addAction("Reload personalities", self._reload_personalities)

    def _current_personality(self) -> Personality:
        key = self._config.get("personality", "opus")
        for persona in self._personalities:
            if persona.key == key:
                return persona
        return builtin_personality("opus")

    def _on_personality_chosen(self, action) -> None:
        self._config.set("personality", action.data())
        persona = self._current_personality()
        self.say(persona.greeting or f"Personality: {persona.name}", look="curious")

    def _open_personalities_folder(self) -> None:
        if sys.platform == "win32":
            os.startfile(str(personalities_dir()))

    def _reload_personalities(self) -> None:
        self._personalities = load_personalities()
        self._rebuild_personality_menu()
        self.say(f"Loaded {len(self._personalities)} personalities.")

    # -- asking ----------------------------------------------------------------- #
    def ask(self, prompt: str) -> None:
        """Handle a line from the prompt box: a /command, or a question for the model."""
        if self._request is not None:
            self.card.show_message("Still busy", "I'm finishing the last answer. Stop it first, or wait a moment.")
            return
        self._needs.interact()
        command, _, rest = prompt.partition(" ")
        command = command.lower()
        if command == "/script":
            self.write_script(rest.strip())
        elif command == "/notes":
            self._ask_notes(rest.strip())
        elif command == "/clip":
            self._show_clip_search(rest.strip())
        elif command == "/timer":
            self._timer_command(rest)
        elif command == "/duck":
            self.toggle_duck_mode()
        else:
            self._start_chat(prompt)

    def _start_chat(self, prompt: str, sent_prompt: str | None = None, sources: list[str] | None = None) -> None:
        duck = self._duck_mode
        persona = builtin_personality("rubber-duck") if duck else self._current_personality()
        request = AskRequest(
            self._settings,
            self._duck_conversation if duck else self._conversation,
            sent_prompt or prompt,
            parent=self,
            system_prompt=self._system_prompt(persona),
            toolbox=None if duck else self._toolbox,  # the duck asks questions; it does not look things up
            history_prompt=prompt,
        )
        request.token.connect(self._on_token)
        request.mood.connect(self._on_mood)
        request.completed.connect(self._on_completed)
        request.failed.connect(self._on_failed)
        request.cancelled.connect(self._on_cancelled)
        self._request = request

        self._pending_sources = sources or []
        self._last_prompt = prompt
        self._card_fade_timer.stop()
        self.card.begin(f"🦆 {prompt}" if duck else prompt)
        self.set_status(STATUS_RUNNING)
        request.start()

    def _system_prompt(self, persona: Personality) -> str:
        """Personality, shared rules, then how the pet feels and what it can do right now."""
        extras = [self._needs.mood_prompt()]
        if network.offline:
            extras.append("Offline mode is on, so you cannot use the internet. Say so if a question needs it.")
        if self._duck_mode:
            extras.append("Rubber duck mode is on: ask one question at a time and do not give answers.")
        extras.append(f"It is {datetime.now():%A %d %B %Y, %H:%M}.")
        return compose_system_prompt(persona.prompt, extras)

    def cancel(self) -> None:
        if self._request is not None:
            self.card.finish("Stopping…")
            self._request.cancel()

    def clear_history(self) -> None:
        self.cancel()
        self._conversation.clear()
        self._duck_conversation.clear()
        self._last_reply = ""
        self.card.show_message("Cleared", "Conversation history is empty again.")

    def show_last(self) -> None:
        if self._last_reply:
            self.card.show_message("Last answer", self._last_reply)
        else:
            self.card.show_message("Nothing yet", "Ask me something first.")

    # -- status ----------------------------------------------------------------- #
    def set_status(self, status: str) -> None:
        """The one place where the pet's look, the status bubble and the tray tooltip follow the state."""
        self._status = status
        self.pet.set_busy(status == STATUS_RUNNING)
        if status == STATUS_RUNNING:
            self.pet.set_expression("thinking")
        elif status == STATUS_NEEDS_INPUT:
            self.pet.set_expression("curious")   # it asked something, so it is curious about the answer
        elif status == STATUS_READY:
            self.pet.set_expression("done")
            self.pet.hop()
        elif status == STATUS_BLOCKED:
            self.pet.set_expression("error")
        else:
            self.pet.set_expression(self._idle_look())

        label = STATUS_LABELS.get(status)
        self.pet.set_glow(label[1] if label else GLYPH)   # the pet glows in the bubble's dot colour
        if label:
            self.status_bubble.set_state(*label)
            self.tray.setToolTip(f"Opus: {label[0]}")
        else:
            self.tray.setToolTip("Opus: offline mode" if network.offline else "Opus: idle")
        self._refresh_status_bubble()

    def _idle_look(self) -> str:
        """The resting look: worried if Ollama is down or the battery is low, otherwise what the pet's needs say."""
        reading = self._stats.latest()
        battery, plugged = reading.get("battery"), reading.get("plugged")
        if not self._health_ok or (battery is not None and battery <= self.LOW_BATTERY and plugged is False):
            return "worried"
        mood = self._needs.mood()
        return mood if mood in ("sleepy", "hungry", "bored") else "idle"

    def _update_idle_look(self) -> None:
        if self._status == STATUS_IDLE:
            look = self._idle_look()
            if self.pet.expression != look:
                self.pet.set_expression(look)

    def _schedule_card_fade(self) -> None:
        """Start the countdown after which a finished answer's card fades away."""
        if self._request is None:
            self._card_fade_timer.start(self.CARD_FADE_DELAY_MS)

    def _maybe_fade_card(self) -> None:
        if self._request is not None or not self.card.isVisible():
            return
        if self.card.frameGeometry().contains(QCursor.pos()):
            # The pointer is on the card, so the user is reading. Check again later.
            self._card_fade_timer.start(self.CARD_HOVER_RECHECK_MS)
            return
        self.card.fade_out()  # when it has faded, the card's closed signal refreshes the status bubble

    def _refresh_status_bubble(self) -> None:
        """Show the bubble only while there is something to report and the card is not already open."""
        wanted = self._status != STATUS_IDLE and not self.card.isVisible() and self.pet.isVisible()
        self.status_bubble.setVisible(wanted)

    def reply(self) -> None:
        """Reply button: close the answer and open the prompt box for a follow-up in the same chat."""
        if self._status in (STATUS_READY, STATUS_NEEDS_INPUT, STATUS_BLOCKED):
            self.set_status(STATUS_IDLE)
        self._card_fade_timer.stop()
        self.card.hide()
        self.open_input()

    def _on_pet_clicked(self) -> None:
        self._needs.interact()
        self.open_thread()

    def open_thread(self) -> None:
        """Clicking the pet or its bubble: bring up the conversation and mark it as seen."""
        if self._request is not None:
            self.card.present()
        elif self._last_reply:
            short = self._last_prompt if len(self._last_prompt) <= 60 else self._last_prompt[:57] + "…"
            self.card.show_message(f"You: {short}", self._last_reply)
            self.card.set_reply_visible(True)
        else:
            self.card.show_message("Opus", "Nothing here yet. Double-click me to ask something.")
        if self._status in (STATUS_READY, STATUS_NEEDS_INPUT, STATUS_BLOCKED):
            self.set_status(STATUS_IDLE)
        self._schedule_card_fade()

    # -- chat worker signals (GUI thread, queued from the worker thread) --------- #
    def _on_token(self, delta: str) -> None:
        self.card.append(delta)

    def _on_mood(self, mood: str) -> None:
        # The status drives the face. The reply's mood tag is hidden from the text and not used.
        pass

    def _on_completed(self, reply: str) -> None:
        self._finish_request()
        if self._pending_sources:
            sources = "\n\nSources: " + ", ".join(self._pending_sources)
            self.card.append(sources)
            reply += sources
            self._pending_sources = []
        self._last_reply = reply
        self._last_said = reply
        self.card.finish("Done")
        if reply.rstrip().endswith("?"):
            self.set_status(STATUS_NEEDS_INPUT)  # the reply asks something, so it waits for the user
        else:
            self.set_status(STATUS_READY)        # unread until the user opens it
        self.card.set_reply_visible(True)
        self._schedule_card_fade()
        self._refresh_dashboard()

    def _on_failed(self, message: str) -> None:
        self._finish_request()
        self._pending_sources = []
        self.card.show_message("Opus hit a snag", message)
        self.set_status(STATUS_BLOCKED)
        self._schedule_card_fade()

    def _on_cancelled(self) -> None:
        self._finish_request()
        self._pending_sources = []
        self.card.finish("Stopped")
        self.set_status(STATUS_IDLE)

    def _finish_request(self) -> None:
        if self._request is not None:
            self._request.deleteLater()
            self._request = None

    # -- tools for the model ------------------------------------------------------- #
    def _build_toolbox(self) -> ToolBox:
        box = ToolBox([
            Tool("search_notes",
                 "Search the user's own notes, PDFs and old projects. Returns excerpts with their file names.",
                 schema({"query": "What to look for"}),
                 lambda args: format_hits(self._knowledge.search(str(args.get("query", "")), 5))),
            Tool("search_clipboard",
                 "Search text the user copied recently. Use since_hours for phrases such as 'yesterday' (48) or 'today'.",
                 schema({"query": "What the copied text was about"},
                        {"since_hours": ("number", "Only look this many hours back")}),
                 self._tool_search_clipboard),
            Tool("system_stats", "Current CPU, memory, disk and battery of the user's computer.",
                 schema(), lambda args: self._stats.describe()),
            Tool("usage_today", "How the user has spent their computer time today, by app, and their current focus stretch.",
                 schema(), lambda args: f"{self._habits.today_summary()}. "
                                        f"Current focus stretch: {format_duration(self._habits.focus_minutes())}."),
            Tool("calculate", "Evaluate an arithmetic expression exactly, for example '(17.5*3)/4' or 'sqrt(2)'.",
                 schema({"expression": "The arithmetic expression"}),
                 lambda args: calculate(str(args.get("expression", "")))),
            Tool("set_timer", "Start a countdown timer or reminder. Opus chimes and says the label when it ends.",
                 {"type": "object",
                  "properties": {"minutes": {"type": "number", "description": "How many minutes"},
                                 "label": {"type": "string", "description": "What the timer is for"}},
                  "required": ["minutes"]},
                 self._tool_set_timer),
        ])
        if self._settings.ollama_api_key:
            box.add(web_search_tool(self._settings))
        return box

    def _tool_search_clipboard(self, args: dict) -> str:
        if not self._config.get("clipboard_history", True):
            return "Clipboard history is turned off."
        try:
            since = float(args["since_hours"]) if args.get("since_hours") not in (None, "") else None
        except (TypeError, ValueError):
            since = None
        return format_clips(self._clipboard.search(str(args.get("query", "")), since))

    def _tool_set_timer(self, args: dict) -> str:
        try:
            minutes = float(args.get("minutes", 0))
        except (TypeError, ValueError):
            minutes = 0.0
        if not 0 < minutes <= 24 * 60:
            return "A timer needs between a few seconds and 24 hours."
        label = str(args.get("label") or "Timer")[:60]
        self._bridge.timer_requested.emit(minutes, label)  # queued to the GUI thread
        return f"Timer set for {format_duration(minutes)}: {label}."

    # -- timers ------------------------------------------------------------------ #
    def start_timer(self, minutes: float, label: str) -> None:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._timer_done(timer, label))
        timer.start(max(1000, int(minutes * 60_000)))
        self._timers.append(timer)
        self.say(f"⏰ Timer set: {label}, {format_duration(minutes)}.")

    def _timer_done(self, timer: QTimer, label: str) -> None:
        if timer in self._timers:
            self._timers.remove(timer)
        timer.deleteLater()
        self.say(f"⏰ {label}: time's up!", look="curious", sound="notify")
        self.notify("Opus timer", f"{label}: time's up!")

    def ask_timer(self) -> None:
        minutes, ok = QInputDialog.getDouble(None, "Set a timer", "Minutes:", 10.0, 0.1, 1440.0, 1)
        if not ok:
            return
        label, ok = QInputDialog.getText(None, "Set a timer", "What is it for?", text="Timer")
        if ok:
            self.start_timer(minutes, label.strip() or "Timer")

    def _timer_command(self, rest: str) -> None:
        """'/timer 10 tea', '/timer 90s eggs', '/timer 1.5h laundry'."""
        match = re.match(r"\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours)?\b\s*(.*)",
                         rest, re.IGNORECASE)
        if not match:
            self.say("Try /timer 10 tea", look="curious")
            return
        value = float(match.group(1))
        unit = (match.group(2) or "m").lower()
        minutes = value / 60 if unit.startswith("s") else value * 60 if unit.startswith("h") else value
        self.start_timer(minutes, match.group(3).strip() or "Timer")

    # -- periodic checks ------------------------------------------------------------ #
    def _on_stats_tick(self) -> None:
        reading = self._stats.sample()
        battery, plugged = reading.get("battery"), reading.get("plugged")
        if battery is not None and plugged is False and battery <= self.LOW_BATTERY:
            if not self._battery_warned:
                self._battery_warned = True
                self.say(f"🔋 Battery at {battery}%. Maybe plug me in?", look="worried", sound="notify")
                self._update_idle_look()
        elif self._battery_warned:
            self._battery_warned = False
            self._update_idle_look()
        self._refresh_dashboard()

    def _on_habit_tick(self) -> None:
        now = time.monotonic()
        elapsed, self._last_habit_tick = now - self._last_habit_tick, now
        idle = self._stats.idle_seconds()
        app, _title, pid = self._stats.foreground_app()
        if pid == os.getpid():
            app = ""  # talking to Opus itself is not time spent in another app
        update = self._habits.tick(app, idle)
        self._needs.tick(min(elapsed, 120.0), self._stats.latest().get("cpu"), idle)
        if update.break_minutes:
            self._needs.break_taken(update.break_minutes)
        if update.nudge:
            self.say(update.nudge, look="curious", sound="notify")
        self._habit_ticks += 1
        if self._habit_ticks % self.SAVE_EVERY_HABIT_TICKS == 0:
            self._habits.save()
            self._config.set("needs", self._needs.state())
        self._update_idle_look()

    def _on_watch_tick(self) -> None:
        for event in self._inbox.poll():
            self._on_inbox_event(event)
        if self._code_watcher is not None:
            for path in self._code_watcher.drain():
                if path not in self._review_queue:
                    self._review_queue.append(path)
        if self._download_watcher is not None:
            for path in self._download_watcher.drain():
                self.say(f"⬇️ Download finished: {path.name}", look="done", sound="success")
        self._next_review()

    def _refresh_dashboard(self) -> None:
        if not self.dashboard.isVisible():
            return
        r = self._stats.latest()

        def pct(value):
            return "—" if value is None else f"{value:.0f}%"

        if r.get("battery") is None:
            battery = "No battery"
        else:
            battery = f"{r['battery']}% · {'charging' if r.get('plugged') else 'on battery'}"
        if self._config.get("calendar"):
            upcoming = describe_event(self._calendar_event)
        else:
            upcoming = "No calendar connected"
        needs = self._needs
        self.dashboard.update_values({
            "cpu": pct(r.get("cpu")),
            "memory": pct(r.get("memory")),
            "battery": battery,
            "next": upcoming,
            "focus": f"{format_duration(self._habits.focus_minutes())} without a break",
            "needs": f"energy {needs.energy:.0f} · fun {needs.fun:.0f} · food {needs.fullness:.0f} ({needs.mood()})",
            "said": self._last_said,
            "offline": network.offline,
        })

    # -- code review on save ---------------------------------------------------------- #
    def _on_watch_action(self) -> None:
        if self._config.get("watch_folder"):
            self.stop_code_watch()
        else:
            self.choose_code_folder()

    def choose_code_folder(self) -> None:
        start = self._config.get("watch_folder") or str(Path.home())
        folder = QFileDialog.getExistingDirectory(None, "Choose a code folder for Opus to watch", start)
        if folder:
            self._start_code_watcher(folder, announce=True)

    def _start_code_watcher(self, folder: str, announce: bool) -> None:
        if self._code_watcher is not None:
            self._code_watcher.stop()
            self._code_watcher = None
        if not Path(folder).is_dir():
            self._config.set("watch_folder", None)
            return
        self._code_watcher = FolderWatcher(folder, CODE_EXTENSIONS)
        self._code_watcher.start()
        self._file_cache.clear()
        self._config.set("watch_folder", folder)
        if announce:
            self.say(f"👀 Watching {Path(folder).name}. I'll speak up only when something looks wrong.", look="curious")

    def stop_code_watch(self) -> None:
        if self._code_watcher is not None:
            self._code_watcher.stop()
            self._code_watcher = None
        self._config.set("watch_folder", None)
        self._review_queue.clear()
        self.say("Stopped watching your code.")

    def _next_review(self) -> None:
        """Review one saved file at a time. Silent unless the model finds a real problem."""
        if self._review_task is not None or not self._review_queue:
            return
        path = self._review_queue.pop(0)
        try:
            if path.stat().st_size > self.MAX_REVIEW_BYTES:
                return
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        previous = self._file_cache.get(path)
        self._file_cache[path] = text
        if previous == text or not text.strip():
            return
        task = ModelTask(self._settings, REVIEW_SYSTEM_PROMPT, build_review_prompt(path, previous, text), parent=self)
        task.review_path = path
        self._review_task = task
        self._start_task(task, self._on_review_done, self._on_review_failed)

    def _on_review_done(self, reply: str) -> None:
        path = getattr(self.sender(), "review_path", None)
        self._review_task = None
        line = parse_review(reply)
        if line and path is not None:
            self.say(f"{path.name}: {line}", look="curious")

    def _on_review_failed(self, message: str) -> None:
        self._review_task = None
        log.info("Code review skipped: %s", message)

    # -- downloads -------------------------------------------------------------------- #
    def _start_download_watcher(self) -> None:
        folder = Path.home() / "Downloads"
        if folder.is_dir() and self._download_watcher is None:
            self._download_watcher = FolderWatcher(folder, None, recursive=False, interval=3.0, settle=2.0,
                                                   new_only=True)
            self._download_watcher.start()

    def _toggle_download_alerts(self, *_) -> None:
        if self._download_watcher is not None:
            self._download_watcher.stop()
            self._download_watcher = None
            self._config.set("download_alerts", False)
            self.say("Download alerts off.")
        else:
            self._start_download_watcher()
            self._config.set("download_alerts", True)
            self.say("I'll tell you when a download finishes.")

    # -- command results from opus_run.py ------------------------------------------------ #
    def _on_inbox_event(self, event: dict) -> None:
        if event.get("kind") != "command":
            return
        command = str(event.get("command", "command"))[:60]
        try:
            code = int(event.get("exit_code", 0))
        except (TypeError, ValueError):
            code = 1
        seconds = event.get("seconds")
        took = f" in {format_duration(seconds / 60)}" if isinstance(seconds, (int, float)) else ""
        if code == 0:
            self.say(f"✅ {command} finished{took}.", look="done", sound="success")
            return
        self.say(f"❌ {command} failed (exit {code}).", look="error", sound="error")
        tail = "\n".join(str(line) for line in (event.get("tail") or []))[-4000:]
        if tail.strip():
            task = ModelTask(self._settings, FAILURE_SYSTEM_PROMPT, f"Command: {command}\nOutput:\n{tail}", parent=self)
            task.command = command
            self._start_task(task, self._on_failure_explained)

    def _on_failure_explained(self, reply: str) -> None:
        command = getattr(self.sender(), "command", "The command")
        line = reply.strip().splitlines()[0][:160] if reply.strip() else ""
        if line:
            self.say(f"❌ {command}: {line}", look="error")

    # -- clipboard ------------------------------------------------------------------------ #
    def _on_clipboard(self) -> None:
        if self._config.get("clipboard_history", True):
            self._clipboard.add(QApplication.clipboard().text())

    def _toggle_clipboard_history(self, *_) -> None:
        enabled = not self._config.get("clipboard_history", True)
        self._config.set("clipboard_history", enabled)
        self.say("Clipboard history on." if enabled else "Clipboard history off. I won't keep new copies.")

    def show_recent_clips(self) -> None:
        self.card.show_message("Recent clips", format_clips(self._clipboard.recent(15)))
        self._schedule_card_fade()

    def _show_clip_search(self, query: str) -> None:
        clips = self._clipboard.search(query) if query else self._clipboard.recent(10)
        self.card.show_message(f"Clipboard: {query or 'recent'}", format_clips(clips))
        self._schedule_card_fade()

    def group_clips(self) -> None:
        clips = self._clipboard.recent(30)
        if len(clips) < 3:
            self.say("Copy a few more things first, then I can group them.", look="curious")
            return
        task = ModelTask(self._settings, TOPIC_SYSTEM_PROMPT, topic_prompt(clips), parent=self, json_format=True)
        task.user_facing = True
        self.say("🗂️ Grouping your recent clips…", look="thinking")
        self._start_task(task, self._on_topics)

    def _on_topics(self, reply: str) -> None:
        topics = parse_topics(reply)
        if not topics:
            self.say("I couldn't make sense of the topics this time. Try again?", look="worried")
            return
        self._clipboard.set_topics(topics)
        counts: dict[str, int] = {}
        for name in topics.values():
            counts[name] = counts.get(name, 0) + 1
        summary = "\n".join(f"• {name}: {count} clip{'s' if count != 1 else ''}"
                            for name, count in sorted(counts.items(), key=lambda item: -item[1]))
        self.card.show_message("Clipboard topics", summary + "\n\nAsk with /clip <topic> to see them.")
        self._schedule_card_fade()

    def clear_clips(self) -> None:
        answer = QMessageBox.question(None, "Clear clipboard history",
                                      f"Delete all {self._clipboard.count()} saved clips? This cannot be undone.")
        if answer == QMessageBox.StandardButton.Yes:
            self._clipboard.clear()
            self.say("Clipboard history cleared.")

    # -- local knowledge base ------------------------------------------------------------- #
    def add_notes_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(None, "Choose a folder of notes, PDFs or old projects")
        if not folder:
            return
        folders = list(self._config.get("notes_folders", []))
        if folder not in folders:
            folders.append(folder)
            self._config.set("notes_folders", folders)
        self.rebuild_notes()

    def rebuild_notes(self) -> None:
        folders = self._config.get("notes_folders", [])
        if not folders:
            self.say("Add a notes folder first: right-click me > My notes > Add a notes folder.", look="curious")
            return
        if self._indexing:
            self.say("I'm already reading your notes.")
            return
        self._indexing = True
        self.say("📚 Reading your notes. I'll tell you when I'm done.", look="thinking")
        self._run_job(self._knowledge.build, self._on_notes_built, list(folders))

    def forget_notes_folders(self) -> None:
        self._config.set("notes_folders", [])
        self._run_job(self._knowledge.build, self._on_notes_built, [])

    def _on_notes_built(self, stats: dict) -> None:
        self._indexing = False
        if not stats.get("files"):
            self.say("My notes index is empty now.")
            return
        message = f"📚 Indexed {stats['files']} files ({stats['chunks']} passages). Ask with /notes."
        if stats.get("skipped_pdfs"):
            message += f" Skipped {stats['skipped_pdfs']} PDFs: run 'pip install pypdf' to include them."
        self.say(message, look="done")

    def _on_knowledge_loaded(self, chunks: int) -> None:
        log.info("Notes index loaded: %d passages", chunks)

    def _ask_notes(self, question: str) -> None:
        if not question:
            self.open_input(prefill="/notes ")
            return
        if not self._knowledge.size():
            self.card.show_message("No notes yet",
                                   "Add a notes folder first: right-click me > My notes > Add a notes folder.")
            self._schedule_card_fade()
            return
        self._pending_question = question
        self.card.show_message(f"You: {question}", "Searching your notes…")
        self._run_job(self._knowledge.search, self._on_notes_hits, question, 5)

    def _on_notes_hits(self, hits: list) -> None:
        """Runs on the GUI thread once the note search has finished on its worker thread."""
        question = self._pending_question
        if not hits:
            self.card.show_message(f"You: {question}", "I couldn't find anything about that in your notes.")
            self._schedule_card_fade()
            return
        sent = f"{NOTES_INSTRUCTIONS}\n\n{format_hits(hits)}\n\nQuestion: {question}"
        sources = list(dict.fromkeys(Path(source).name for _, source, _ in hits))
        self._start_chat(question, sent_prompt=sent, sources=sources)

    # -- mini-scripts ----------------------------------------------------------------------- #
    def write_script(self, task_text: str) -> None:
        if not task_text:
            self.open_input(prefill="/script ")
            return
        start = self._config.get("script_folder") or str(Path.home())
        folder = QFileDialog.getExistingDirectory(None, "Which folder should the script work in?", start)
        if not folder:
            return
        self._config.set("script_folder", folder)
        try:
            listing = ", ".join(sorted(os.listdir(folder))[:40])
        except OSError:
            listing = "(could not list the folder)"
        task = ModelTask(self._settings, SCRIPT_SYSTEM_PROMPT,
                         f"Task: {task_text}\nWorking folder: {folder}\nSome of its files: {listing}",
                         parent=self, temperature=0.1)
        task.user_facing = True
        task.script_task = task_text
        task.script_folder = folder
        self.say("✍️ Writing a script. I'll show it to you before anything runs.", look="thinking")
        self._start_task(task, self._on_script_written)

    def _on_script_written(self, reply: str) -> None:
        task = self.sender()
        code = extract_python(reply)
        if not code:
            self.say("I couldn't write a script for that. Try describing it differently.", look="worried")
            return
        dialog = ScriptDialog(task.script_task, task.script_folder, code)
        dialog.run_requested.connect(self._run_script)
        self._script_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _run_script(self) -> None:
        dialog = self._script_dialog
        if dialog is None:
            return
        path = save_script(dialog.code)
        self._run_job(run_script, self._on_script_ran, path, dialog.folder)

    def _on_script_ran(self, result: dict) -> None:
        if self._script_dialog is not None:
            self._script_dialog.show_result(result)
        if result.get("exit_code") == 0:
            self.say("✅ The script finished. The output is in the script window.", look="done", sound="success")
        else:
            self.say("❌ The script failed. Check the output in the script window.", look="error", sound="error")

    # -- modes and needs -------------------------------------------------------------------- #
    def toggle_duck_mode(self, *_) -> None:
        self._duck_mode = not self._duck_mode
        self.input.set_mode(self._duck_mode)
        if self._duck_mode:
            self._duck_conversation.clear()
            duck = builtin_personality("rubber-duck")
            self.say(f"🦆 {duck.greeting or 'Rubber duck mode on.'}", look="curious")
            self.open_input()
        else:
            self.say("Rubber duck mode off.")

    def feed(self) -> None:
        self._needs.feed()
        self._config.set("needs", self._needs.state())
        self.say(random.choice(["Yum! Thank you.", "Crunchy. Thanks!", "That hit the spot."]), look="done")
        self._update_idle_look()

    def _toggle_sounds(self, *_) -> None:
        enabled = not self._config.get("sounds", True)
        self._config.set("sounds", enabled)
        self.say("Sounds on." if enabled else "Sounds off.")

    def _toggle_offline(self, *_) -> None:
        offline = not network.offline
        network.set_offline(offline)
        self._config.set("offline", offline)
        self.pet.set_offline_badge(offline)
        if offline:
            message = "✈️ Offline mode on. I won't contact anything outside this computer."
            if not network.allows(self._settings.ollama_url):
                message += " Your Ollama server isn't local, so I can't answer until you go back online."
        else:
            message = "Back online."
        self.say(message, look="curious")
        self.set_status(self._status)  # refresh the tooltip
        self.check_health()

    # -- dashboard and calendar ------------------------------------------------------------- #
    def toggle_dashboard(self, *_) -> None:
        self._set_dashboard(not self.dashboard.isVisible())

    def _set_dashboard(self, show: bool) -> None:
        self._config.set("dashboard", show)
        if show:
            self.dashboard.show()
            self._refresh_dashboard()
            self.dashboard.anchor_beside(self.pet.frameGeometry())
        else:
            self.dashboard.hide()

    def choose_calendar(self) -> None:
        text, ok = QInputDialog.getText(
            None, "Connect a calendar",
            "Path or URL of an .ics calendar.\nIn Google Calendar or Outlook, copy the 'secret address in iCal format'.",
            text=self._config.get("calendar", ""))
        if ok:
            self._config.set("calendar", text.strip())
            self._refresh_calendar()

    def _refresh_calendar(self) -> None:
        source = self._config.get("calendar", "")
        if not source:
            self._calendar_event = None
            return
        self._run_job(next_calendar_event, self._on_calendar, source)

    def _on_calendar(self, event) -> None:
        self._calendar_event = event
        self._refresh_dashboard()
        self._check_calendar_reminder()

    def _check_calendar_reminder(self) -> None:
        """Say so once when the next event starts within ten minutes."""
        if not self._calendar_event or not self._config.get("calendar"):
            return
        start, title = self._calendar_event
        minutes = (start - datetime.now()).total_seconds() / 60
        key = f"{start.isoformat()}|{title}"
        if 0 <= minutes <= 10 and key not in self._reminded:
            self._reminded.add(key)
            self.say(f"📅 {title} starts in {max(1, round(minutes))} minute{'s' if round(minutes) != 1 else ''}.",
                     look="curious", sound="notify")

    def show_help(self) -> None:
        self.card.show_message("Opus help", HELP_TEXT)
        self._schedule_card_fade()

    # -- move mode and flicks ------------------------------------------------------------------ #
    def start_move_mode(self) -> None:
        """Hotkey: the pet follows the mouse. Click to drop it, flick to send it to an edge, Esc to cancel."""
        if self._moving:
            return
        if not self.pet.isVisible():
            self.toggle_pet()
        self._moving = True
        self._move_started = time.monotonic()
        self._move_origin = self.pet.pos()
        self._move_samples = []
        self._move_timer.start(15)
        self.say("Move mode: click to drop me, flick to send me to an edge, flick down to dismiss me, Esc to cancel.")

    def _move_tick(self) -> None:
        now = time.monotonic()
        cursor = QCursor.pos()
        if key_down(VK_ESCAPE):
            self._end_move()
            self.pet.move(self._move_origin)
            return
        if key_down(VK_LBUTTON):
            self._end_move()
            self.pet.swallow_next_click()  # the drop click lands on the pet; do not open the chat
            return
        if now - self._move_started > self.MOVE_TIMEOUT_S:
            self._end_move()
            return
        self.pet.move(cursor - QPoint(PET_W // 2, PET_H // 2))
        self._move_samples.append((now, cursor))
        self._move_samples = [(t, p) for t, p in self._move_samples if now - t <= 0.2]
        if now - self._move_started > 0.3:
            vx, vy = drag_velocity(self._move_samples)
            if math.hypot(vx, vy) >= PetWidget.FLICK_SPEED:
                self._end_move()
                self._on_flick(vx, vy)

    def _end_move(self) -> None:
        self._moving = False
        self._move_timer.stop()

    def _on_flick(self, vx: float, vy: float) -> None:
        """A fast flick: down dismisses the pet to the tray; any other direction sends it to that edge."""
        if vy > 0 and vy > abs(vx) * 1.2:
            if self.pet.isVisible():
                self.toggle_pet()
            self.notify("Opus is hiding", "Click the tray icon, or press the hotkey, to bring me back.")
            return
        area = (QApplication.screenAt(self.pet.frameGeometry().center()) or QApplication.primaryScreen()).availableGeometry()
        x, y = self.pet.x(), self.pet.y()
        if abs(vx) >= abs(vy) * 0.5:
            x = area.left() + 12 if vx < 0 else area.right() - PET_W - 12
        if vy < 0 and abs(vy) >= abs(vx) * 0.5:
            y = area.top() + 40
        y = max(area.top() + 40, min(y, area.bottom() - PET_H - 40))
        self._glide_pet(QPoint(x, y))

    def _glide_pet(self, target: QPoint) -> None:
        animation = QPropertyAnimation(self.pet, b"pos", self)
        animation.setDuration(280)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.setStartValue(self.pet.pos())
        animation.setEndValue(target)
        animation.start()
        self._glide = animation  # keep it alive until it finishes
        self.pet.react("curious", 900)

    # -- health ---------------------------------------------------------------------------------- #
    def check_health(self, announce: bool = False) -> None:
        worker = HealthCheck(self._settings, parent=self)
        worker.announce = announce  # read back in _on_health via sender()
        worker.result.connect(self._on_health)
        worker.start()

    def _on_health(self, ok: bool, message: str) -> None:
        worker = self.sender()
        announce = bool(getattr(worker, "announce", False))
        if worker is not None:
            worker.deleteLater()

        self._health_ok = ok
        self.tray.setToolTip(f"Opus: {message}")
        if announce:
            self.card.show_message("Ollama check", message)
        if not ok and not self._health_warned:
            self._health_warned = True
            self.notify("Ollama not reachable", message)
        elif ok:
            self._health_warned = False
        self._update_idle_look()

    # -- shutdown ------------------------------------------------------------------------------- #
    def shutdown(self) -> None:
        """Connected to aboutToQuit. Release hotkeys, stop work, save state, hide the tray icon."""
        for hotkey in self._hotkeys:
            hotkey.unregister()
        self.cancel()
        for watcher in (self._code_watcher, self._download_watcher):
            if watcher is not None:
                watcher.stop()
        self._habits.save()
        self._config.set("needs", self._needs.state())
        self._save_position()
        self.tray.hide()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def enable_dpi_awareness() -> None:
    """Ask Windows for per-monitor DPI awareness. Qt may already have set it, and failure is harmless."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        log.debug("Could not set DPI awareness context")


_INSTANCE_LOCK = None  # kept alive for the whole run


def claim_single_instance() -> bool:
    """
    Only one Opus may run at a time. Two copies fight over the hotkeys and look frozen.
    Returns False if another copy already holds the lock.
    """
    global _INSTANCE_LOCK
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    _INSTANCE_LOCK = kernel32.CreateMutexW(None, False, "OpusPetSingleInstance")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def main() -> int:
    if not claim_single_instance():
        print("Opus is already running. Look for its icon in the tray.", file=sys.stderr)
        return 0
    enable_dpi_awareness()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Opus")
    app.setQuitOnLastWindowClosed(False)  # the pet is a background app

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("System tray is unavailable; Opus will run without a tray icon.")

    controller = OpusController(load_settings())
    app.aboutToQuit.connect(controller.shutdown)

    # Let Ctrl+C reach Python while the Qt event loop is running.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    pump = QTimer()
    pump.start(250)
    pump.timeout.connect(lambda: None)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
