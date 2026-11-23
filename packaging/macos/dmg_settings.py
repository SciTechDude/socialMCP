"""Disk-image window layout, read by dmgbuild.

    dmgbuild -s dmg_settings.py -D app=<path> -D background=<png> \
        -D version=<v> "SocialMCP <v>" out.dmg

The point of all this: dragging an app to Applications is a file copy, so
macOS shows no progress and announces nothing when it finishes. A bare file
listing — which is what this image used to be — gives a first-time user no
instruction and no feedback, and reads as "the download did nothing". The
background, the arrow and the two icon positions are the whole instruction.

Everything here is geometry that has to agree with `dmg_background.js`, which
draws the arrow between the two icon positions named below.
"""

from __future__ import annotations

from pathlib import Path

# dmgbuild execs this file with a prepared namespace and reads the
# module-level names back out afterwards. Values passed with -D arrive in a
# `defines` dict, NOT as bare globals — reading them as globals yields empty
# strings, and the build then succeeds while producing a 6 KB disk image with
# no application in it. The `.get` default keeps this file importable on its
# own, which is what lets a test read the geometry below.
_defines = globals().get("defines", {})
app = _defines.get("app", "")
background = _defines.get("background") or None
version = _defines.get("version", "")
licence = _defines.get("licence", "")
readme = _defines.get("readme", "")

application = Path(app).name if app else "SocialMCP.app"

# --- contents -------------------------------------------------------------
# The terms sit beside the app, readable before it is even copied across.
files = [path for path in (app, licence, readme) if path]
symlinks = {"Applications": "/Applications"}

# --- window ---------------------------------------------------------------
# Matches the background image's dimensions exactly; a mismatch silently tiles
# or crops it. The height has to accommodate two icon rows and the text band
# between them: at 400 the second row of icons reached y=394 and collided with
# the instruction text.
window_rect = ((200, 180), (600, 500))

# 112, not 128: an icon is drawn centred on its position and its filename sits
# below that, so a row occupies icon_size + a label. Four rows' worth of
# that plus the text band is what sets the window height above.
icon_size = 112
text_size = 13
default_view = "icon-view"
show_icon_preview = False

# Chrome off. A sidebar or toolbar in an install window invites browsing and
# shifts the content away from where the background expects it.
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False

# --- icon positions ------------------------------------------------------
# Horizontal centres at 150 and 450; the two rows sit above and below the
# instruction text. `dmg_background.js` declares the same two row positions
# (ICON_ROW1_TOP, ICON_ROW2_TOP) and draws its arrow along the first, so
# these numbers move together and a test checks they still agree.
ICON_ROW1_TOP = 150
ICON_ROW2_TOP = 400

icon_locations = {
    application: (150, ICON_ROW1_TOP),
    "Applications": (450, ICON_ROW1_TOP),
    "LICENSE.txt": (150, ICON_ROW2_TOP),
    "README-AND-PRIVACY.txt": (450, ICON_ROW2_TOP),
}

# --- format ---------------------------------------------------------------
# UDZO is what ships; dmgbuild handles the read-write staging image itself.
format = "UDZO"
