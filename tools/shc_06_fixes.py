#!/usr/bin/env python3
"""
shc_06_fixes.py - 追加の修正 (ShuiCast)。05 の後に適用する

  1. Comment    接続中も Comment を編集できる (配信には送らない)
  2. 証明書     保存した指紋と違う証明書が来たら、新しい証明書を信頼するか確認する (標準は「いいえ」)
  3. AAC        一時バッファを広げ、上限を超えて書かない
  4. AAC+       エンコーダー削除時に、DLL を解放する前に部品を解放する
  5. AAC+       設定フォルダのパスが長くても ini のパスがあふれない

使い方:  python tools/shc_06_fixes.py trunk
冪等。アンカーが見つからなければエラーで停止。
"""

import patchlib as P

TAG = "shuicast_fixes"
P.init(TAG)
from patchlib import sub_once, sub_n, patch_file, c_wide  # noqa: E402


# 1. Comment は接続中も編集できる
def fix_comment(t):
    mark = TAG + ": comment"
    if mark in t:
        return t
    return sub_once(
        t,
        r"[ \t]*/\* shuicast_ui: comment: locked while connected[^\n]*\n[ \t]*\{\n(?:[^\n]*\n){2}[ \t]*\}\n",
        lambda m: "\t/* " + mark + ": the comment stays editable while connected */\n",
        "comment lock",
    )


# 2. 保存した指紋と違う証明書
CHANGED_MSG = (
    "保存してある証明書と違う証明書が届きました。\n\n"
    "サーバー: %ls\n保存した指紋:\n%ls\n今回の指紋:\n%ls\n\n"
    "サーバーの証明書を更新した場合は変わります。心当たりがない場合は、偽のサーバーの可能性があるので「いいえ」を選んでください。\n\n"
    "新しい証明書を信頼して、以後この証明書だけを許可しますか？"
)


def fix_cert(t):
    mark = TAG + ": cert"
    if mark in t:
        return t
    # no more silent refusal: only log, then ask like the first time (but with the old fingerprint shown)
    t = sub_once(
        t,
        r"(if \(g->m_TLSPinned\[0\] != '\\000'\) \{\n[^\n]*LOG_ERROR[^\n]*\n)[ \t]*return 0;\n",
        lambda m: m.group(1).replace("To trust a new certificate, delete ServerTLSTrustedCert in the config file.", "Asking whether to trust it."),
        "cert pinned",
    )
    new = (
        "\t/* " + mark + ": a different certificate than the trusted one */\n"
        "\tif (g->m_TLSPinned[0] != '\\000') {\n"
        "\t\tWCHAR\twold[100];\n"
        "\t\tMultiByteToWideChar(CP_ACP, 0, g->m_TLSPinned, -1, wold, 99);\n"
        "\t\twold[99] = 0;\n"
        '\t\t_snwprintf(msg, 1499, L"' + c_wide(CHANGED_MSG) + '", whost, wold, wfp);\n'
        "\t\tmsg[1499] = 0;\n"
        "\t}\n"
    )
    return sub_once(t, r"(?m)^[ \t]*answer = MessageBoxW\(", lambda m: new + m.group(0), "cert message")


# 3. AAC の一時バッファ
def fix_fifo(t):
    mark = TAG + ": fifo"
    if mark in t:
        return t
    t = sub_once(t, r"(m_AACSamplesInput \* sizeof\(\s*float\s*\) \* )16\b",
                 lambda m: m.group(1) + "64 /* " + mark + " */", "fifo size")
    return sub_once(t, r"for\s*\(\s*int i = 0; i < numsamples; i\+\+\s*\)(?=\s*\{\s*m_AACFIFO\[currentFIFO\])",
                    lambda m: "for (int i = 0; i < numsamples && currentFIFO < (int) (m_AACSamplesInput * 64); i++)", "fifo limit")


# 4. 削除時の AAC+ 部品の解放
def fix_release(t):
    mark = TAG + ": release"
    if mark in t:
        return t
    new = (
        "#if HAVE_AACP\n"
        "    // " + mark + ": the object lives in the DLL, so delete it before the DLL is freed\n"
        "    if ( m_AACPEncoder )\n    {\n        delete m_AACPEncoder;\n        m_AACPEncoder = NULL;\n    }\n"
        "#endif\n"
    )
    return sub_once(t, r"(CEncoder::~CEncoder \(\)(?s:.*?)pthread_mutex_destroy\( &m_Mutex \);\n)",
                    lambda m: m.group(1) + new, "destructor")


# 5. 長いフォルダ名
def fix_ini_path(t):
    mark = TAG + ": ini path"
    if mark in t:
        return t
    return sub_n(
        t,
        r'wsprintf\(conf_file, ("%s\\\\shuicast_(?:fh)?aacp_%d\.ini"), defaultConfigDir, encoderNumber\);',
        lambda m: "_snprintf(conf_file, sizeof(conf_file) - 1, " + m.group(1) + ", defaultConfigDir, encoderNumber);   // " + mark,
        "ini path", count=2,
    )


patch_file("shuicast/BasicSettings.cpp", fix_comment)
patch_file("libshuicast/libshuicast.cpp", lambda t: fix_ini_path(fix_release(fix_fifo(fix_cert(t)))))
print(TAG + ": done")
