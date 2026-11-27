#!/usr/bin/env python3
"""
shc_02_stability.py - 安定性の修正 (ShuiCast)

  [録音・保存]
    1. fflush         アーカイブ書き込み5か所でフラッシュ (クラッシュしても直前まで残る)
    2. モノラル録音   MP3 (LAME) のノイズだけの問題 / Ogg の歪んで低くなる問題を修正
    3. 録音復旧       デバイスが失われたら 3秒おき・最大20回まで再オープン
                      (スタンドアロン版のみ)
  [接続中のクラッシュ防止]
    4. 接続中ロック   接続中は Encoder Type / Bitrate / Quality / Samplerate /
                      Channels をグレーアウト (変更するとクラッシュした)
    5. 多重起動警告   同じフォルダ (= 同じ .cfg) のインスタンスが起動中なら確認
  [設定ファイル]
    6. バックアップ   変更前を .bak1 / .bak2 (2世代) に残す。一度 .new に書いてから置き換え。書き込みに失敗したら置き換えない

  [設定ウィンドウ]
    7. ロックの追従   設定ウィンドウを開いたまま接続 / 切断しても、すぐグレーアウト / 解除 (0.5秒ごと)。
                      接続中に OK を押しても、ロック対象の項目は接続中の値のまま変えない

使い方:  python tools/shc_02_stability.py trunk
冪等 (何度当てても同じ)。アンカーが見つからなければエラーで停止。
"""

import re
import sys
from pathlib import Path

import patchlib as P

TAG = "shuicast_stability"
P.init(TAG)
SRC = P.SRC
from patchlib import fail, sub_once, sub_n, after, patch_file, c_wide  # noqa: E402,F401


# ----------------------------------------------------------------------
# 設定値 (録音復旧の間隔と回数)
# ----------------------------------------------------------------------

RETRY_INTERVAL_MS = 3000   # wait between two attempts to re-open the device


MAX_TRIES = 20             # attempts in a row before giving up (20 x 3 s = about 1 minute)


# ----------------------------------------------------------------------
# 1. fflush
# ----------------------------------------------------------------------

MARK_FFLUSH = "shuicast_stability: fflush"


def fix_fflush(t):
    if MARK_FFLUSH in t:
        print("shuicast_stability: fflush already present, skipped")
        return t
    # MP3 / Ogg archive (one-line if in SendToServer)
    t = sub_n(
        t,
        r"([ \t]*)if \( m_SaveDirectoryFlag && m_SaveFilePtr && !m_SaveAsWAV \) fwrite\( data, length, 1, m_SaveFilePtr \);\n",
        lambda m: m.group(0)
        + m.group(1)
        + "if ( m_SaveDirectoryFlag && m_SaveFilePtr && !m_SaveAsWAV ) fflush( m_SaveFilePtr );\t/* "
        + MARK_FFLUSH + " */\n",
        "libshuicast.cpp archive fwrite (MP3/Ogg)",
    )
    # WAV archive (three sample writes, each inside braces)
    t = sub_n(
        t,
        r"([ \t]*)fwrite\( int_samples, sizeofData, 1, m_SaveFilePtr \);\n",
        lambda m: m.group(0) + m.group(1) + "fflush( m_SaveFilePtr );\n",
        "libshuicast.cpp archive fwrite (WAV)",
        count=4,
    )
    print("shuicast_stability: fflush added (5 places)")
    return t


# ----------------------------------------------------------------------
# 2. モノラル録音
# ----------------------------------------------------------------------

MARK_MONO = "shuicast_stability: mono"


def fix_mono(t):
    """DoEncoding() ALWAYS receives interleaved stereo (for a mono encoder both
    channels carry the same signal). The LAME and Ogg mono branches did not take that
    into account: LAME wrote to a fixed index (-> noise only), Ogg consumed the
    interleaved pairs as consecutive mono samples (-> half speed, low and distorted)."""
    if MARK_MONO in t:
        print("shuicast_stability: mono fix already present, skipped")
        return t
    # LAME (lame_enc.dll): the output index was never advanced
    t = sub_n(
        t,
        r"(if \( m_CurrentChannels == 1 \)[^\n]*\n\s*\{\n)(\s*)samplecount = numsamples;\n"
        r"(\s*for \( int i = 0; i < numsamples \* 2; i \+= 2 \)\n\s*\{\n\s*)"
        r"int_samples\[samplecount\] = \(short\)\(samples\[i\] \* 32767\.0\);\n",
        lambda m: m.group(1) + m.group(2) + "samplecount = 0;\t/* " + MARK_MONO + " */\n"
        + m.group(3) + "int_samples[samplecount++] = (short)(samples[i] * 32767.0);\n",
        "libshuicast.cpp LAME mono branch",
    )
    # Ogg Vorbis: skip the duplicated right channel
    t = sub_n(
        t,
        r"(if \( m_CurrentChannels == 1 \)\n\s*\{\n\s*while \( samplecounter < numsamples \)\n\s*\{\n)"
        r"(\s*)buffer\[0\]\[samplecounter\] = \*\(src\+\+\);\n",
        lambda m: m.group(1) + m.group(2) + "buffer[0][samplecounter] = *(src++);\n"
        + m.group(2) + "src++;\t/* " + MARK_MONO + ": skip the duplicated channel */\n",
        "libshuicast.cpp Ogg mono branch",
    )
    print("shuicast_stability: mono fix added (LAME, Ogg)")
    return t


# ----------------------------------------------------------------------
# 3. 録音復旧
# ----------------------------------------------------------------------

MARK_REC ="shuicast_stability: recording device recovery"


H_DECL = "    void RecoverRecording();\t// " + MARK_REC + "\n"


CPP_CALL = "    pWindow->RecoverRecording();\t/* " + MARK_REC + " */\n"


CPP_DEF = """/*
 * shuicast_stability: recording device recovery (standalone build only).
 * Called once a second. When the recording device is lost the BASS record channel
 * stops while m_LiveRecRunning is still set. Re-open the device, but only every
 * RETRY_MS and at most MAX_TRIES times in a row, so a device that stays away does not
 * cause a flood of init attempts and status messages. A successful restart (by this
 * function or by the user selecting the device again) re-arms it.
 */
void CMainWindow::RecoverRecording ()
{
#ifdef SHUICASTSTANDALONE
    const DWORD RETRY_MS  = %(retry)d;
    const int   MAX_TRIES = %(tries)d;
    static bool  lost = false;
    static bool  gaveUp = false;
    static int   tries = 0;
    static DWORD lastTry = 0;

    if ( !::IsWindow( m_hWnd ) ) return;

    if ( !lost )
    {
        if ( m_LiveRecRunning && BASS_ChannelIsActive( inRecHandle ) == BASS_ACTIVE_STOPPED )
        {
            lost = true;
            gaveUp = false;
            tries = 0;
            generalStatusCallback( (void *) "Recording device lost - trying to recover", FILE_LINE );
        }
        else
        {
            return;
        }
    }
    else if ( BASS_ChannelIsActive( inRecHandle ) != BASS_ACTIVE_STOPPED )
    {
        lost = false;
        gaveUp = false;
        tries = 0;
        return;
    }

    if ( gaveUp ) return;

    if ( tries >= MAX_TRIES )
    {
        gaveUp = true;
        generalStatusCallback( (void *) "Recording device lost - gave up retrying. Select the device again to resume.", FILE_LINE );
        return;
    }

    if ( tries > 0 && GetTickCount() - lastTry < RETRY_MS ) return;

    tries++;
    lastTry = GetTickCount();
    StopRecording();
    StartRecording();

    if ( BASS_ChannelIsActive( inRecHandle ) != BASS_ACTIVE_STOPPED )
    {
        lost = false;
        gaveUp = false;
        tries = 0;
        generalStatusCallback( (void *) "Recording device recovered", FILE_LINE );
    }
#endif
}

""" % {"retry": RETRY_INTERVAL_MS, "tries": MAX_TRIES}


def fix_rec_header(t):
    if MARK_REC in t:
        print("shuicast_stability: header already patched, skipped")
        return t
    return sub_n(
        t,
        r"([ \t]*void DoStartRecording\( bool restart = false \);\n)",
        lambda m: m.group(1) + H_DECL,
        "MainWindow.h DoStartRecording declaration",
    )


def fix_rec_cpp(t):
    if MARK_REC in t:
        print("shuicast_stability: recording recovery already present, skipped")
        return t
    t = sub_n(
        t,
        r"([ \t]*if \( needRestart \) pWindow->DoStartRecording\( true \);\n)",
        lambda m: m.group(1) + CPP_CALL,
        "MainWindow.cpp MetadataCheckTimer",
    )
    t = sub_n(
        t,
        r"(/\* CMainWindow dialog \*/\n)",
        lambda m: CPP_DEF + m.group(1),
        "MainWindow.cpp CMainWindow dialog marker",
    )
    print("shuicast_stability: recording recovery added (retry every %d ms, max %d tries)"
          % (RETRY_INTERVAL_MS, MAX_TRIES))
    return t


if not CPP_DEF.isascii():
    fail("internal: inserted C++ must be ASCII")


# ----------------------------------------------------------------------
# 4. 接続中ロック
# ----------------------------------------------------------------------

MARK_LOCK = TAG + ": lock"


def fix_lock_header(t):
    if MARK_LOCK in t:
        return t
    return sub_once(
        t,
        r"([ \t]*)void UpdateFields\s*\(\);\n",
        lambda m: m.group(0)
        + m.group(1) + "void UpdateFieldsCore();   // %s\n" % MARK_LOCK
        + m.group(1) + "void ApplyEncoderLock();\n"
        + m.group(1) + "BOOL m_LockEncoder = FALSE;   // TRUE while the encoder is connected\n",
        "BasicSettings.h UpdateFields declaration",
    )


LOCK_CODE = """void CBasicSettings::UpdateFields ()
{
	UpdateFieldsCore();
	ApplyEncoderLock();
}

// %(m)s: while connected, the fields that change the running encoder are greyed out
void CBasicSettings::ApplyEncoderLock ()
{
	CWnd *w;

	if ( !m_LockEncoder )
	{
		m_EncoderTypeCtrl.EnableWindow( TRUE );
		w = GetDlgItem( IDC_SAMPLERATE );
		if ( w != NULL ) w->EnableWindow( TRUE );
		return;
	}
	m_EncoderTypeCtrl.EnableWindow( FALSE );
	m_BitrateCtrl.EnableWindow( FALSE );
	m_QualityCtrl.EnableWindow( FALSE );
	m_UseBitrateCtrl.EnableWindow( FALSE );
	m_ChannelsCtrl.EnableWindow( FALSE );
	w = GetDlgItem( IDC_SAMPLERATE );
	if ( w != NULL ) w->EnableWindow( FALSE );
}

void CBasicSettings::UpdateFieldsCore ()
{"""


def fix_lock_cpp(t):
    if MARK_LOCK in t:
        return t
    return sub_once(
        t,
        r"void CBasicSettings::UpdateFields\s*\(\)\s*\{",
        lambda m: LOCK_CODE % {"m": MARK_LOCK},
        "BasicSettings.cpp UpdateFields",
    )


def fix_lock_config(t):
    if MARK_LOCK in t:
        return t
    return sub_once(
        t,
        r"([ \t]*)basicSettings->UpdateFields\(\);\n",
        lambda m: m.group(1)
        + "basicSettings->m_LockEncoder = (encoder->IsConnected() != 0);   // %s\n" % MARK_LOCK
        + m.group(0),
        "Config.cpp GlobalsToDialog UpdateFields",
    )


# ----------------------------------------------------------------------
# 5. 多重起動警告
# ----------------------------------------------------------------------

MARK_INST = TAG + ": instance"


INST_TITLE = "多重起動の確認"


INST_MSG = (
    "同じフォルダ(設定ファイルの場所)を使うインスタンスが、すでに起動しています。\n\n"
    "設定ファイル(.cfg)を互いに上書きし合い、設定が壊れたり、"
    "意図しない内容で保存されたりするおそれがあります。\n\n"
    "それでも起動しますか?\n\n"
    "(別々に使いたい場合は、アプリのフォルダごとコピーして、別のフォルダから起動してください。)"
)


def instance_block(indent, dir_expr, prefix):
    lines = [
        "/* %s: warn when another instance uses the same folder (= the same .cfg files) */" % MARK_INST,
        "{",
        "\tchar instDir[MAX_PATH] = \"\";",
        "\tchar *instFilePart = NULL;",
        "\tchar instMutexName[MAX_PATH + 64];",
        "\tif (GetFullPathNameA(%s, MAX_PATH, instDir, &instFilePart) == 0) {" % dir_expr,
        "\t\tstrcpy(instDir, \".\");",
        "\t}",
        "\tfor (char *ip = instDir; *ip; ++ip) {",
        "\t\tif (*ip == '\\\\' || *ip == ':') *ip = '/';",
        "\t\telse if (*ip >= 'A' && *ip <= 'Z') *ip = (char)(*ip - 'A' + 'a');",
        "\t}",
        "\tsprintf(instMutexName, \"Local\\\\%s_inst_%%s\", instDir);" % prefix,
        "\tHANDLE instMutex = CreateMutexA(NULL, FALSE, instMutexName);",
        "\tif (instMutex != NULL && GetLastError() == ERROR_ALREADY_EXISTS) {",
        "\t\tif (MessageBoxW(NULL, L\"%s\", L\"%s\", MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2) != IDYES) {"
        % (c_wide(INST_MSG), c_wide(INST_TITLE)),
        "\t\t\treturn FALSE;",
        "\t\t}",
        "\t}",
        "}",
    ]
    return "".join(indent + ln + "\n" for ln in lines)


def fix_instance(t):
    if MARK_INST in t:
        return t
    return sub_once(
        t,
        r"([ \t]*)AfxEnableControlContainer\(\);\n",
        lambda m: m.group(0) + "\n" + instance_block(m.group(1), "\".\"", "shuicast"),
        "shuicast_standalone.cpp AfxEnableControlContainer",
    )


# ----------------------------------------------------------------------
# 6. 設定ファイルのバックアップ
# ----------------------------------------------------------------------

MARK_BAK = TAG + ": backup"


BACKUP_FUNC = """/*
 * %s: config file backups.
 * The new config is written to <file>.new first. When it differs from the current file, the
 * current file is kept as <file>.bak1 (the old .bak1 becomes .bak2). This is done once per
 * run and file, so many saves in one run do not push out the older backups.
 * Replacing the file from the finished .new file also protects against a half-written config.
 */
static void CommitConfigFile ( const char *configFile, const char *tmpFile )
{
    static char  backedUp[32][1100];
    static int   numBackedUp = 0;
    FILE        *fa = fopen( configFile, "rb" );
    FILE        *fb = fopen( tmpFile, "rb" );
    int          existed = ( fa != NULL );
    int          same = 0;
    int          i;

    if ( fa != NULL && fb != NULL )
    {
        same = 1;
        for ( ;; )
        {
            int ca = fgetc( fa );
            int cb = fgetc( fb );

            if ( ca != cb )
            {
                same = 0;
                break;
            }
            if ( ca == EOF )
            {
                break;
            }
        }
    }
    if ( fa != NULL ) fclose( fa );
    if ( fb != NULL ) fclose( fb );
    if ( same )
    {
        remove( tmpFile );
        return;
    }
    if ( existed )
    {
        int already = 0;

        for ( i = 0; i < numBackedUp; i++ )
        {
            if ( !strcmp( backedUp[i], configFile ) ) already = 1;
        }
        if ( !already && numBackedUp < 32 )
        {
            char bak1[1100];
            char bak2[1100];

            strcpy( backedUp[numBackedUp++], configFile );
            sprintf( bak1, "%%s.bak1", configFile );
            sprintf( bak2, "%%s.bak2", configFile );
            CopyFileA( bak1, bak2, FALSE );
            CopyFileA( configFile, bak1, FALSE );
        }
    }
    if ( !MoveFileExA( tmpFile, configFile, MOVEFILE_REPLACE_EXISTING | MOVEFILE_COPY_ALLOWED ) )
    {
        // keep the .new file when the copy fails too: it is the only copy of the new settings
        if ( CopyFileA( tmpFile, configFile, FALSE ) ) remove( tmpFile );
    }
}

"""


# a write error (disk full etc.) must not replace the good config with a truncated one
# (the first line takes the indentation of the replaced fclose line)
CLOSE_AND_COMMIT = (
    "int writeError = ferror( filep );",
    "if ( fclose( filep ) != 0 ) writeError = 1;",
    "if ( writeError )",
    "{",
    "    remove( tmpFile );",
    "    LogMessage( LOG_ERROR, \"Cannot write config file %s\\n\", configFile );",
    "}",
    "else CommitConfigFile( configFile, tmpFile );",
)


def fix_backup(t):
    if MARK_BAK in t:
        return t
    t = sub_once(
        t,
        r"void CEncoder::WriteConfigFile \(\)\n\{\n",
        lambda m: (BACKUP_FUNC % MARK_BAK) + m.group(0),
        "libshuicast.cpp WriteConfigFile",
    )
    t = sub_once(
        t,
        r'([ \t]*)FILE \*filep = fopen\( configFile, "w" \);\n',
        lambda m: (
            m.group(1) + "char_t tmpFile[1100] = \"\";\n"
            + m.group(1) + "sprintf( tmpFile, \"%s.new\", configFile );\n"
            + m.group(1) + "FILE *filep = fopen( tmpFile, \"w\" );\n"
        ),
        "libshuicast.cpp config fopen",
    )
    t = sub_once(
        t,
        r'([ \t]*)fclose\( filep \);\n([ \t]*\}\n[ \t]*else\n[ \t]*\{\n[ \t]*LogMessage\( LOG_ERROR, "Cannot open config file)',
        lambda m: m.group(0).replace(
            "fclose( filep );\n", "".join((m.group(1) if i else "") + ln + "\n" for i, ln in enumerate(CLOSE_AND_COMMIT)), 1
        ),
        "libshuicast.cpp config fclose",
    )
    return t


# ----------------------------------------------------------------------
# 7. 設定ウィンドウを開いたまま接続 / 切断したときのロック
# ----------------------------------------------------------------------

MARK_WATCH = TAG + ": lockwatch"

TIMER_FUNC = """/* %s: the settings window may stay open while the encoder connects or disconnects,
 * so the lock is checked twice a second while the window is visible */
void CConfig::OnTimer(UINT_PTR nIDEvent)
{
	if (nIDEvent == 9701) {
		if (IsWindowVisible() && basicSettings != NULL && basicSettings->GetSafeHwnd() != NULL && parentDialog != NULL) {
			CMainWindow *pwin = (CMainWindow *)parentDialog;
			BOOL now = pwin->IsEncoderConnected(m_CurrentEnc);

			if (now != basicSettings->m_LockEncoder) {
				basicSettings->m_LockEncoder = now;
				if (now) {
					basicSettings->ApplyEncoderLock();
				}
				else {
					basicSettings->UpdateData(TRUE);
					basicSettings->UpdateFields();
				}
			}
		}
		return;
	}
	CDialog::OnTimer(nIDEvent);
}

""" % MARK_WATCH

IS_CONNECTED_FUNC = """/* %s: is the encoder with this (1-based) number connected right now? */
BOOL CMainWindow::IsEncoderConnected(int enc) {
	return (enc >= 1 && enc <= gMain.GetNumEncoders() && enc <= MAX_ENCODERS && gEncoders[enc - 1] != NULL && gEncoders[enc - 1]->IsConnected() != 0) ? TRUE : FALSE;
}

""" % MARK_WATCH

SNAPSHOT = """		CEncoder *lk = gEncoders[enc - 1];
		// %s: while connected, the locked fields keep the running values (even if they were edited before connecting)
		bool lk_held = (lk->IsConnected() != 0);
		auto lk_sr = lk->m_CurrentSamplerate;
		auto lk_br = lk->m_CurrentBitrate;
		auto lk_ch = lk->m_CurrentChannels;
		auto lk_et = lk->GetEncoderType();
		bool lk_ub = lk->GetUseBitrate();
		char lk_type[sizeof(lk->m_EncodeType)], lk_aq[sizeof(lk->m_AACQuality)], lk_oq[sizeof(lk->m_OggQuality)];
		memcpy(lk_type, lk->m_EncodeType, sizeof(lk_type));
		memcpy(lk_aq, lk->m_AACQuality, sizeof(lk_aq));
		memcpy(lk_oq, lk->m_OggQuality, sizeof(lk_oq));
		pConfig->DialogToGlobals(lk);
		if (lk_held) {
			lk->m_CurrentSamplerate = lk_sr;
			lk->m_CurrentBitrate = lk_br;
			lk->m_CurrentChannels = lk_ch;
			lk->SetEncoderType(lk_et);
			lk->SetUseBitrate(lk_ub);
			memcpy(lk->m_EncodeType, lk_type, sizeof(lk_type));
			memcpy(lk->m_AACQuality, lk_aq, sizeof(lk_aq));
			memcpy(lk->m_OggQuality, lk_oq, sizeof(lk_oq));
		}"""


def fix_watch_config_h(t):
    if MARK_WATCH in t:
        return t
    return after(t, r"[ \t]*afx_msg void OnClose\(\);[^\n]*\n",
                 "\tafx_msg void OnTimer(UINT_PTR nIDEvent);   /* " + MARK_WATCH + " */\n", "Config.h OnClose")


def fix_watch_config_cpp(t):
    if MARK_WATCH in t:
        return t
    t = sub_once(t, r"(BEGIN_MESSAGE_MAP\(CConfig, CDialog\)[\s\S]*?[ \t]*ON_WM_CLOSE\(\)[^\n]*\n)",
                 lambda m: m.group(1) + "\tON_WM_TIMER()   /* " + MARK_WATCH + " */\n", "Config message map")
    t = sub_once(t, r"(BOOL CConfig::OnInitDialog\s*\(\s*\)\s*\{\s*CDialog::OnInitDialog\(\);[^\n]*\n)",
                 lambda m: m.group(1) + "\tSetTimer(9701, 500, NULL);   /* " + MARK_WATCH + " */\n", "Config OnInitDialog")
    t = sub_once(t, r"void CConfig::OnOK\s*\(\s*\)", lambda m: TIMER_FUNC + m.group(0), "Config OnOK")
    return t


def fix_watch_main_h(t):
    if MARK_WATCH in t:
        return t
    return after(t, r"[ \t]*void ProcessConfigDone\(int enc, CConfig \*pConfig\);[^\n]*\n",
                 "    BOOL IsEncoderConnected(int enc);   /* " + MARK_WATCH + " */\n", "MainWindow.h ProcessConfigDone")


def fix_watch_main_cpp(t):
    if MARK_WATCH in t:
        return t
    t = sub_once(t, r"void CMainWindow::ProcessConfigDone\s*\(", lambda m: IS_CONNECTED_FUNC + m.group(0), "ProcessConfigDone")
    t = sub_once(t, r"[ \t]*pConfig->DialogToGlobals\(\s*gEncoders\[enc - 1\]\s*\);", lambda m: SNAPSHOT % MARK_WATCH, "DialogToGlobals call")
    return t


# ----------------------------------------------------------------------
# apply
# ----------------------------------------------------------------------

patch_file("shuicast/Config.cpp", fix_lock_config)
patch_file("shuicast/BasicSettings.h", fix_lock_header)
patch_file("shuicast/BasicSettings.cpp", fix_lock_cpp)
patch_file("shuicast/shuicast_standalone.cpp", fix_instance)
patch_file("libshuicast/libshuicast.cpp", lambda t: fix_backup(fix_mono(fix_fflush(t))))
patch_file("shuicast/MainWindow.h", fix_rec_header)
patch_file("shuicast/MainWindow.cpp", fix_rec_cpp)
patch_file("shuicast/Config.h", fix_watch_config_h)
patch_file("shuicast/Config.cpp", fix_watch_config_cpp)
patch_file("shuicast/MainWindow.h", fix_watch_main_h)
patch_file("shuicast/MainWindow.cpp", fix_watch_main_cpp)
print(TAG + ": done")
