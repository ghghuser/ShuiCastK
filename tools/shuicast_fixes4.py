#!/usr/bin/env python3
"""
shuicast_fixes4.py - fourth batch of ShuiCast changes (applied at build time).

  Encoder comment: a personal note per encoder ("what is this encoder set for").
    * Basic Settings: new "Comment" field below "Attenuation"
      (the page and the dialog are made 20 dialog units taller; the Multi ASIO
       group box and its two combo boxes move down by the same amount)
    * main window list: new last column "Comment" (shown as Unicode, so Japanese is fine)
    * stored in the encoder's config file as EncoderComment=...
      (up to 255 bytes; the field accepts 200 bytes = 100 full-width characters)
    * it is NEVER sent to the server or written into the stream
    * the field is greyed out while the encoder is connected (same as the other locked fields)
    * old settings keep working: a saved column order / width list with 5 columns is
      accepted and the new column is appended at the end

Usage:  python tools/shuicast_fixes4.py trunk
Idempotent, fails loudly if an anchor is missing.
"""
import re
import sys
from pathlib import Path

TAG = "shuicast_fixes4"
MARK = TAG + ": comment"
GROW = 20           # dialog units added to the Basic Settings page


def fail(msg):
    print(TAG + ": ERROR: " + msg)
    sys.exit(1)


if len(sys.argv) != 2:
    fail("usage: shuicast_fixes4.py <trunk dir>")
ROOT = Path(sys.argv[1])
if not ROOT.is_dir():
    fail("trunk dir not found: " + str(ROOT))


def sub_once(text, pattern, repl, label, flags=0):
    new, n = re.subn(pattern, repl, text, flags=flags)
    if n != 1:
        fail("%s: expected exactly 1 match, found %d" % (label, n))
    return new


def after(text, pattern, insert, label):
    return sub_once(text, pattern, lambda m: m.group(0) + insert, label)


def patch_file(rel, func):
    path = ROOT / rel
    if not path.exists():
        fail("%s not found under %s" % (rel, ROOT))
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


def grow(m):
    return m.group(1) + str(int(m.group(2)) + GROW)


# ------------------------------------------------------------------ resource.h
def p_resource(t):
    if "IDC_ENCCOMMENT" in t:
        return t
    return after(
        t,
        r"#define IDC_ATTENUATION[^\n]*\n",
        "#define IDC_ENCCOMMENT                  1990\n",
        "resource.h IDC_ATTENUATION",
    )


# ------------------------------------------------------------------ shuicast.rc
def rc_block(t, header, func, label):
    m = re.search(r"^" + header + r".*?^END", t, re.S | re.M)
    if not m:
        fail("shuicast.rc: block not found: " + label)
    return t[: m.start()] + func(m.group(0)) + t[m.end():]


def p_rc(t):
    if "IDC_ENCCOMMENT" in t:
        return t

    def config_dlg(b):
        b = sub_once(b, r"(IDD_CONFIG DIALOGEX \d+, \d+, \d+, )(\d+)", grow, "rc IDD_CONFIG height")
        b = sub_once(b, r'(IDC_TAB1,"SysTabControl32",WS_TABSTOP,\d+,\d+,\d+,)(\d+)', grow, "rc tab height")
        b = sub_once(b, r'(DEFPUSHBUTTON\s+"OK",IDOK,\d+,)(\d+)', grow, "rc OK button")
        b = sub_once(b, r'(PUSHBUTTON\s+"Cancel",IDCANCEL,\d+,)(\d+)', grow, "rc Cancel button")
        return b

    def basic_page(b):
        b = sub_once(b, r"(IDD_PROPPAGE_LARGE DIALOGEX \d+, \d+, \d+, )(\d+)", grow, "rc page height")
        b = sub_once(b, r'(GROUPBOX\s+"General Settings",IDC_STATIC,\d+,\d+,\d+,)(\d+)', grow, "rc group height")
        # Multi ASIO group box and its combo boxes move down
        b = sub_once(b, r'(GROUPBOX\s+"Multi ASIO Channels",IDC_ASIOGROUPBOX,\d+,)(\d+)', grow, "rc ASIO group")
        b = sub_once(b, r"(COMBOBOX\s+IDC_MULTIASIOCHANNEL,\d+,)(\d+)", grow, "rc ASIO combo 1")
        b = sub_once(b, r"(COMBOBOX\s+IDC_MULTIASIOCHANNEL2,\d+,)(\d+)", grow, "rc ASIO combo 2")
        m = re.search(r"EDITTEXT\s+IDC_ATTENUATION,\d+,(\d+),\d+,14[^\n]*\n", b)
        if not m:
            fail("shuicast.rc: IDC_ATTENUATION not found")
        y = int(m.group(1)) + 17
        b = b[: m.end()] + "    EDITTEXT        IDC_ENCCOMMENT,112,%d,190,14,ES_AUTOHSCROLL\n" % y + b[m.end():]
        return after(
            b,
            r'    RTEXT\s+"Attenuation",IDC_STATIC,\d+,\d+,\d+,8\n',
            '    RTEXT           "Comment",IDC_STATIC,33,%d,70,8\n' % (y + 2),
            "rc Attenuation label",
        )

    t = rc_block(t, r"IDD_CONFIG DIALOGEX", config_dlg, "IDD_CONFIG")
    t = rc_block(t, r"IDD_PROPPAGE_LARGE DIALOGEX", basic_page, "IDD_PROPPAGE_LARGE")
    return t


# ------------------------------------------------------------ BasicSettings.h/.cpp
def p_bs_h(t):
    if "m_EncComment" in t:
        return t
    return sub_once(
        t,
        r"([ \t]*)CString([ \t]+)m_Attenuation;\n",
        lambda m: m.group(0) + m.group(1) + "CString" + m.group(2) + "m_EncComment;   // " + MARK + "\n",
        "BasicSettings.h m_Attenuation",
    )


def p_bs_cpp(t):
    if MARK in t:
        return t
    t = after(
        t,
        r'\tm_Attenuation = _T\(""\);\n',
        '\tm_EncComment = _T("");   /* ' + MARK + ' */\n',
        "BasicSettings.cpp ctor",
    )
    t = after(
        t,
        r"\tDDX_Text\(pDX, IDC_ATTENUATION, m_Attenuation\);\n",
        "\tDDX_Text(pDX, IDC_ENCCOMMENT, m_EncComment);   // " + MARK + "\n",
        "BasicSettings.cpp DDX",
    )
    t = after(
        t,
        r"\tCDialog::OnInitDialog\(\);\n",
        "\t/* " + MARK + ": at most 200 bytes (100 full-width characters) */\n"
        "\t{\n"
        "\t\tCWnd *pComment = GetDlgItem(IDC_ENCCOMMENT);\n"
        "\t\tif (pComment != NULL) {\n"
        "\t\t\tpComment->SendMessage(EM_LIMITTEXT, 200, 0);\n"
        "\t\t}\n"
        "\t}\n",
        "BasicSettings.cpp OnInitDialog",
    )
    t = after(
        t,
        r"void CBasicSettings::ApplyEncoderLock \(\)\n\{\n\tCWnd \*w;\n",
        "\t/* " + MARK + ": locked while connected, like the other fields */\n"
        "\t{\n"
        "\t\tCWnd *wc = GetDlgItem( IDC_ENCCOMMENT );\n"
        "\t\tif ( wc != NULL ) wc->EnableWindow( m_LockEncoder ? FALSE : TRUE );\n"
        "\t}\n",
        "BasicSettings.cpp ApplyEncoderLock",
    )
    return t


# ------------------------------------------------------------------- Config.cpp
def p_config(t):
    if "m_EncComment" in t:
        return t
    t = sub_once(
        t,
        r"([ \t]*)basicSettings->m_Attenuation = encoder->m_AttenuationTable;\n",
        lambda m: m.group(0) + m.group(1) + "basicSettings->m_EncComment = encoder->m_EncComment;   /* " + MARK + " */\n",
        "Config.cpp dialog <- encoder",
    )
    t = sub_once(
        t,
        r"([ \t]*)strcpy\( encoder->m_AttenuationTable, LPCSTR\( basicSettings->m_Attenuation \) \);\n",
        lambda m: m.group(0)
        + m.group(1) + "strncpy( encoder->m_EncComment, LPCSTR( basicSettings->m_EncComment ), sizeof( encoder->m_EncComment ) - 1 );   /* " + MARK + " */\n"
        + m.group(1) + "encoder->m_EncComment[sizeof( encoder->m_EncComment ) - 1] = '\\000';\n",
        "Config.cpp encoder <- dialog",
    )
    return t


# ------------------------------------------------------------------ libshuicast
def p_lib_h(t):
    if "m_EncComment" in t:
        return t
    return after(
        t,
        r"[ \t]*char_t[ \t]+m_ServGenre\[100\][^\n]*\n",
        "    char_t                m_EncComment[256]         = {};   // " + MARK + ": personal note, never sent to the server\n",
        "libshuicast.h m_ServGenre",
    )


def p_lib_cpp(t):
    if "EncoderComment" in t:
        return t
    t = after(
        t,
        r'    GetConfigVariable\( gAppName, "ServerGenre",[^\n]*\n',
        '    GetConfigVariable( gAppName, "EncoderComment", "", m_EncComment, sizeof( m_EncComment ), NULL );   /* ' + MARK + ' */\n',
        "libshuicast.cpp GetConfigVariable",
    )
    t = after(
        t,
        r'    PutConfigVariable\( gAppName, "ServerGenre", m_ServGenre \);\n',
        '    PutConfigVariable( gAppName, "EncoderComment", m_EncComment );   /* ' + MARK + ' */\n',
        "libshuicast.cpp PutConfigVariable",
    )
    t = after(
        t,
        r'    AddConfigVariable\( "ServerGenre" \);\n',
        '    AddConfigVariable( "EncoderComment" );\n',
        "libshuicast.cpp AddConfigVariable",
    )
    return t


# ----------------------------------------------------------------- MainWindow.cpp
COL_BLOCK = (
    "\t\t{\n"
    "\t\t\t/* " + MARK + ": the comment is set as Unicode text so that Japanese is shown correctly */\n"
    "\t\t\tWCHAR\tcnew[300];\n"
    "\t\t\tWCHAR\tcold[300];\n"
    "\t\t\tLVITEMW\tclvi;\n"
    "\n"
    "\t\t\tMultiByteToWideChar(CP_ACP, 0, gEncoders[i]->m_EncComment, -1, cnew, 299);\n"
    "\t\t\tcnew[299] = 0;\n"
    "\t\t\tcold[0] = 0;\n"
    "\t\t\tmemset(&clvi, 0, sizeof(clvi));\n"
    "\t\t\tclvi.iSubItem = 5;\n"
    "\t\t\tclvi.pszText = cold;\n"
    "\t\t\tclvi.cchTextMax = 299;\n"
    "\t\t\t::SendMessageW(m_Encoders.m_hWnd, LVM_GETITEMTEXTW, (WPARAM) i, (LPARAM) &clvi);\n"
    "\t\t\tif (wcscmp(cold, cnew) != 0) {\n"
    "\t\t\t\tclvi.iSubItem = 5;\n"
    "\t\t\t\tclvi.pszText = cnew;\n"
    "\t\t\t\t::SendMessageW(m_Encoders.m_hWnd, LVM_SETITEMTEXTW, (WPARAM) i, (LPARAM) &clvi);\n"
    "\t\t\t}\n"
    "\t\t}\n"
)


def p_main(t):
    if MARK in t:
        return t
    t = sub_once(
        t,
        r"#define LIST_NUMCOLS 5\n",
        lambda m: "#define LIST_NUMCOLS 6   /* " + MARK + ": 5 -> 6 (Comment column) */\n",
        "MainWindow.cpp LIST_NUMCOLS",
    )
    t = after(
        t,
        r"    m_Encoders\.SetColumnWidth\( 4, 140 \);\n",
        '    m_Encoders.InsertColumn( 5, "Comment" );   // ' + MARK + "\n"
        "    m_Encoders.SetColumnWidth( 5, 200 );\n",
        "MainWindow.cpp Comment column",
    )
    t = sub_once(
        t,
        r"\tif \(ok && n == LIST_NUMCOLS && order\[0\] == 0\) \{\n",
        lambda m: "\t/* " + MARK + ": a column order saved with one column less stays valid (the new column goes last) */\n"
        "\tif (ok && n == LIST_NUMCOLS - 1 && !used[LIST_NUMCOLS - 1]) {\n"
        "\t\torder[n++] = LIST_NUMCOLS - 1;\n"
        "\t}\n"
        + m.group(0),
        "MainWindow.cpp column order",
    )
    t = sub_once(
        t,
        r"\t\tif \(m_Encoders\.GetCheck\(i\) != want\) \{\n",
        lambda m: COL_BLOCK + m.group(0),
        "MainWindow.cpp UpdateExtraColumns",
    )
    return t


patch_file("shuicast/resource.h", p_resource)
patch_file("shuicast/shuicast.rc", p_rc)
patch_file("shuicast/BasicSettings.h", p_bs_h)
patch_file("shuicast/BasicSettings.cpp", p_bs_cpp)
patch_file("shuicast/Config.cpp", p_config)
patch_file("libshuicast/libshuicast.h", p_lib_h)
patch_file("libshuicast/libshuicast.cpp", p_lib_cpp)
patch_file("shuicast/MainWindow.cpp", p_main)
print(TAG + ": done")
