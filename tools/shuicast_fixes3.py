#!/usr/bin/env python3
"""
shuicast_fixes3.py - third batch of ShuiCast fixes (applied at build time).

  1. Live Recording: volume slider / input list with speakers (loopback devices)
       Loopback ("speakers") devices have no input list and no input volume control,
       so the slider did nothing. Now the slider falls back to a software volume
       (0..100 %) applied to the recorded samples. The same fallback is used when an
       input has no volume control. A status line tells when it is used.

Usage:  python tools/shuicast_fixes3.py trunk
Idempotent (MARK_* strings), fails loudly if an anchor is missing.
"""
import re
import sys
from pathlib import Path

TAG = "shuicast_fixes3"


def fail(msg):
    print(TAG + ": ERROR: " + msg)
    sys.exit(1)


if len(sys.argv) != 2:
    fail("usage: shuicast_fixes3.py <source dir>")
SRC = Path(sys.argv[1])
if not SRC.is_dir():
    fail("source dir not found: " + str(SRC))


def sub_once(text, pattern, repl, label, flags=0):
    new, n = re.subn(pattern, repl, text, flags=flags)
    if n != 1:
        fail("%s: expected exactly 1 match, found %d" % (label, n))
    return new


def patch_file(rel, func):
    path = SRC / rel
    if not path.exists():
        fail("%s not found under %s" % (rel, SRC))
    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    text = raw.decode("latin-1").replace("\r\n", "\n")
    new = func(text)
    if new == text:
        return
    if crlf:
        new = new.replace("\n", "\r\n")
    path.write_bytes(new.encode("latin-1"))
    print(TAG + ": patched " + rel)


# ------------------------------------------------------------ 2. live recording volume
MARK_REC = TAG + ": rec"

REC_HELPERS = r'''/* __MARK__: software volume for recording devices without an input volume control (loopback / speakers) */
static volatile int s_recSoftOn = 0;
static volatile int s_recSoftVol = 100;

static float RecSoftGain() {
	return s_recSoftOn ? ((float) s_recSoftVol / 100.0f) : 1.0f;
}

static int RecCountInputs() {
	int n = 0;

	while (BASS_RecordGetInputName(n) != NULL) {
		n++;
	}
	return n;
}

static void RecSoftMode(bool on) {
	s_recSoftVol = 100;
	s_recSoftOn = on ? 1 : 0;
}

/* tries the real input volume first; if the input does not exist or has no volume control, the software volume is used */
static void RecApplyVolume(int input, int percent) {
	BOOL ok = FALSE;

	if (percent < 0) {
		percent = 0;
	}
	if (percent > 100) {
		percent = 100;
	}
	if (input >= 0 && BASS_RecordGetInputName(input) != NULL) {
		ok = BASS_RecordSetInput(input, BASS_INPUT_ON, (float) percent / 100.0f);
	}
	if (ok) {
		RecSoftMode(false);
	}
	else {
		s_recSoftVol = percent;
		s_recSoftOn = 1;
	}
}

'''.replace("__MARK__", MARK_REC)

# placed right after the input list of the selected device has been read (BASS device initialised)
REC_DEVICE_CHANGED = (
    "\t/* " + MARK_REC + ": no inputs (loopback / speakers) -> the slider is a software volume */\n"
    "\tif (RecCountInputs() == 0) {\n"
    "\t\tRecSoftMode(true);\n"
    "\t\tm_RecVolume = 100;\n"
    "\t\tgeneralStatusCallback((void *) \"This recording device has no input volume control: the slider works as a software volume\", FILE_LINE);\n"
    "\t}\n"
    "\telse {\n"
    "\t\tRecSoftMode(false);\n"
    "\t}\n"
)

REC_STARTUP = (
    "    /* " + MARK_REC + ": no inputs (loopback / speakers) -> the slider is a software volume */\n"
    "    if (RecCountInputs() == 0) {\n"
    "        RecSoftMode(true);\n"
    "        m_RecVolume = 100;\n"
    "    }\n"
    "    else {\n"
    "        RecSoftMode(false);\n"
    "    }\n"
)


def fix_rec_main(t):
    if MARK_REC in t:
        return t

    # (a) helpers before the record callback
    t = sub_once(
        t,
        r"(#if \( BASSVERSION == 0x203 \)\nBOOL CALLBACK BASSwaveInputProc)",
        lambda m: REC_HELPERS + m.group(1),
        "MainWindow.cpp BASSwaveInputProc",
    )

    # (b) software gain on the recorded samples (16-bit path; USE_BASS_FLOAT is 0)
    t = sub_once(
        t,
        r"(samples\[i\] = sample / 32767\.f;)",
        lambda m: "samples[i] = sample / 32767.f * RecSoftGain();   /* " + MARK_REC + " */",
        "MainWindow.cpp sample conversion",
    )

    # (c) slider
    t = sub_once(
        t,
        r"BASS_RecordSetInput\( m_CurrentInput, BASS_INPUT_ON, \(float\)\(\(float\)m_RecVolume/100\) \);",
        lambda m: "RecApplyVolume( m_CurrentInput, m_RecVolume );   /* " + MARK_REC + " */",
        "MainWindow.cpp OnHScroll volume",
    )

    # (d) start-up: the input list has just been read for the selected device
    t = sub_once(
        t,
        r"([ \t]*//BASS_RecordSetDevice\(m_CurrentInputCard\);\n)",
        lambda m: m.group(1) + REC_STARTUP,
        "MainWindow.cpp start-up",
    )

    # (e) card changed in the combo box: right after the combo box of inputs is cleared
    t = sub_once(
        t,
        r"([ \t]*m_RecDevicesCtrl\.ResetContent\(\);\n)",
        lambda m: m.group(1) + REC_DEVICE_CHANGED,
        "MainWindow.cpp OnSelchangeReccards",
    )
    return t


patch_file("shuicast/MainWindow.cpp", fix_rec_main)
print(TAG + ": done")
