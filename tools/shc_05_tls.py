#!/usr/bin/env python3
"""
shc_05_tls.py - TLS (HTTPS) 対応 (ShuiCast)

  * Basic Settings の Server Port の右に「Use TLS (HTTPS)」を追加
  * Windows 標準の SChannel (TLS 1.2)。追加DLLは不要。tlsclient.h をこのスクリプトが生成
  * 証明書は Windows の証明書ストアとホスト名で検証。失敗したときは SHA-256 指紋を
    表示して1回だけ確認し、「はい」なら指紋を固定 (ServerTLSTrustedCert)
  * 暗号化の対象: 配信の送信、パスワード認証、曲名の更新 (admin 経由)
  * 設定ファイルのキー: ServerTLS=1 / ServerTLSTrustedCert=<SHA-256指紋>

使い方:  python tools/shc_05_tls.py trunk
冪等。アンカーが見つからなければエラーで停止。
"""

import re
import sys
from pathlib import Path

import patchlib as P

TAG = "shuicast_tls"
P.init(TAG)
SRC = P.SRC
from patchlib import fail, sub_once, sub_n, after, patch_file, c_wide  # noqa: E402,F401


# ----------------------------------------------------------------------
# TLS
# ----------------------------------------------------------------------

MARK_TLS = TAG + ": tls"


TLSCLIENT_H = r'''/*
 * tlsclient.h - minimal TLS 1.2 client for the Win32 sockets used by this encoder,
 * built on the Windows SChannel (SSPI). No extra DLLs are needed.
 *
 *   tlsc_attach(sock, host, ...)  run the TLS handshake on a connected socket and
 *                                 verify the server certificate
 *   tlsc_send / tlsc_recv         like send() / recv(); plain sockets pass through
 *   tlsc_close(sock)              TLS shutdown (if any) and closesocket()
 *
 * Certificate check: the Windows certificate store and the host name are used.
 * A certificate that fails the check is accepted only if its SHA-256 fingerprint is
 * the pinned one, or the trust callback returns non-zero (the app asks the user and
 * then remembers the fingerprint).
 */
#ifndef TLSCLIENT_H_INCLUDED
#define TLSCLIENT_H_INCLUDED

#ifndef SECURITY_WIN32
#define SECURITY_WIN32
#endif
#include <windows.h>   /* the sockets of this program use the winsock.h API that windows.h brings in */
#include <wincrypt.h>
#include <schannel.h>
#include <security.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _MSC_VER
#pragma comment(lib, "secur32.lib")
#pragma comment(lib, "crypt32.lib")
#endif

#ifndef SCH_USE_STRONG_CRYPTO
#define SCH_USE_STRONG_CRYPTO 0x00400000
#endif
#ifndef SCH_CRED_NO_SERVERNAME_CHECK
#define SCH_CRED_NO_SERVERNAME_CHECK 0x00000004
#endif
#ifndef SP_PROT_TLS1_2_CLIENT
#define SP_PROT_TLS1_2_CLIENT 0x00000800
#endif
#ifndef CERT_SHA256_HASH_PROP_ID
#define CERT_SHA256_HASH_PROP_ID 107
#endif

#define TLSC_MAX_CONN 64
#define TLSC_INBUF_SIZE 40000

typedef int (*tlsc_trust_fn)(void *ctx, const char *fingerprint, unsigned long certError);

typedef struct tlsc_conn_s {
    SOCKET          sock;
    CredHandle      cred;
    CtxtHandle      ctx;
    int             haveCred;
    int             haveCtx;
    SecPkgContext_StreamSizes sizes;
    unsigned char  *inbuf;      /* encrypted bytes received, not yet decrypted */
    int             inlen;
    unsigned char  *plain;      /* decrypted bytes not yet handed out */
    int             plainlen;
    int             plainoff;
    unsigned char  *outbuf;
    int             outcap;
    int             refs;
    int             dead;
    int             ready;      /* handshake finished */
    CRITICAL_SECTION cs;
} tlsc_conn;

static tlsc_conn *g_tlsc_table[TLSC_MAX_CONN];
static volatile LONG g_tlsc_lock = 0;

static void tlsc_lock(void)
{
    while (InterlockedCompareExchange(&g_tlsc_lock, 1, 0) != 0) {
        Sleep(0);
    }
}

static void tlsc_unlock(void)
{
    InterlockedExchange(&g_tlsc_lock, 0);
}

static void tlsc_free_conn(tlsc_conn *c)
{
    if (c->haveCtx) {
        DeleteSecurityContext(&c->ctx);
    }
    if (c->haveCred) {
        FreeCredentialsHandle(&c->cred);
    }
    if (c->inbuf) free(c->inbuf);
    if (c->plain) free(c->plain);
    if (c->outbuf) free(c->outbuf);
    DeleteCriticalSection(&c->cs);
    free(c);
}

/* find the connection of a socket and take a reference to it. A connection that is
   being closed is returned too (the caller then fails instead of sending plain text). */
static tlsc_conn *tlsc_acquire(SOCKET s)
{
    tlsc_conn *found = NULL;
    int i;

    tlsc_lock();
    for (i = 0; i < TLSC_MAX_CONN; i++) {
        if (g_tlsc_table[i] != NULL && g_tlsc_table[i]->sock == s) {
            found = g_tlsc_table[i];
            if (!found->dead) {
                break;   /* a live connection wins over one that is being closed */
            }
        }
    }
    if (found != NULL) {
        found->refs++;
    }
    tlsc_unlock();
    return found;
}

static void tlsc_release(tlsc_conn *c)
{
    int freeit = 0;

    tlsc_lock();
    c->refs--;
    if (c->refs <= 0 && c->dead) {
        freeit = 1;
    }
    tlsc_unlock();
    if (freeit) {
        tlsc_free_conn(c);
    }
}

static int tlsc_raw_send_all(SOCKET s, const unsigned char *p, int len)
{
    int done = 0;

    while (done < len) {
        int n = send(s, (const char *) p + done, len - done, 0);
        if (n <= 0) {
            return -1;
        }
        done += n;
    }
    return done;
}

static void tlsc_hex(const unsigned char *in, int n, char *out)
{
    static const char digits[] = "0123456789ABCDEF";
    int i;

    for (i = 0; i < n; i++) {
        out[i * 2] = digits[in[i] >> 4];
        out[i * 2 + 1] = digits[in[i] & 15];
    }
    out[n * 2] = '\000';
}

static void tlsc_fingerprint(PCCERT_CONTEXT cert, char *hex65)
{
    unsigned char hash[32];
    DWORD len = sizeof(hash);

    hex65[0] = '\000';
    if (CertGetCertificateContextProperty(cert, CERT_SHA256_HASH_PROP_ID, hash, &len) && len == 32) {
        tlsc_hex(hash, 32, hex65);
    }
}

/* Windows certificate check (store + host name). Returns 0 when the certificate is trusted. */
static unsigned long tlsc_check_chain(PCCERT_CONTEXT cert, const char *host)
{
    CERT_CHAIN_PARA            cp;
    PCCERT_CHAIN_CONTEXT       chain = NULL;
    HTTPSPolicyCallbackData    https;
    CERT_CHAIN_POLICY_PARA     pp;
    CERT_CHAIN_POLICY_STATUS   ps;
    WCHAR                      whost[256];
    LPSTR                      usages[] = { (LPSTR) szOID_PKIX_KP_SERVER_AUTH, (LPSTR) szOID_SERVER_GATED_CRYPTO, (LPSTR) szOID_SGC_NETSCAPE };
    unsigned long              result;

    memset(&cp, 0, sizeof(cp));
    cp.cbSize = sizeof(cp);
    cp.RequestedUsage.dwType = USAGE_MATCH_TYPE_OR;
    cp.RequestedUsage.Usage.cUsageIdentifier = 3;
    cp.RequestedUsage.Usage.rgpszUsageIdentifier = usages;

    if (!CertGetCertificateChain(NULL, cert, NULL, cert->hCertStore, &cp, 0, NULL, &chain)) {
        return GetLastError() ? GetLastError() : 0x80090300;
    }

    whost[0] = 0;
    MultiByteToWideChar(CP_ACP, 0, host, -1, whost, 255);
    whost[255] = 0;

    memset(&https, 0, sizeof(https));
    https.cbStruct = sizeof(https);
    https.dwAuthType = AUTHTYPE_SERVER;
    https.fdwChecks = 0;
    https.pwszServerName = whost;

    memset(&pp, 0, sizeof(pp));
    pp.cbSize = sizeof(pp);
    pp.pvExtraPolicyPara = &https;

    memset(&ps, 0, sizeof(ps));
    ps.cbSize = sizeof(ps);

    if (!CertVerifyCertificateChainPolicy(CERT_CHAIN_POLICY_SSL, chain, &pp, &ps)) {
        result = GetLastError() ? GetLastError() : 0x80090300;
    }
    else {
        result = ps.dwError;
    }
    CertFreeCertificateChain(chain);
    return result;
}

/*
 * Handshake on a connected socket. Returns 1 on success (the socket is then TLS
 * wrapped), 0 on failure (err gets a short English reason; the socket is left open).
 * fpOut (at least 65 bytes) receives the server certificate fingerprint when known.
 * certErrOut receives the Windows certificate error when the certificate was not
 * trusted by the store (0 = trusted or pinned).
 */
static int tlsc_attach(SOCKET s, const char *host, const char *pinned, tlsc_trust_fn trust, void *trustCtx,
                       char *fpOut, unsigned long *certErrOut, char *err, int errlen)
{
    tlsc_conn      *c;
    SCHANNEL_CRED   scred;
    TimeStamp       ts;
    SECURITY_STATUS ss;
    DWORD           flags;
    DWORD           attrs = 0;
    int             first = 1;
    int             i;
    int             ok = 0;
    PCCERT_CONTEXT  cert = NULL;
    char            fp[80] = "";
    unsigned long   certErr = 0;

    if (fpOut) fpOut[0] = '\000';
    if (certErrOut) *certErrOut = 0;
    if (err && errlen > 0) err[0] = '\000';

    c = (tlsc_conn *) calloc(1, sizeof(tlsc_conn));
    if (c == NULL) {
        _snprintf(err, errlen, "out of memory");
        return 0;
    }
    InitializeCriticalSection(&c->cs);
    c->sock = s;
    c->refs = 2;   /* one for the table, one for this function */
    c->inbuf = (unsigned char *) malloc(TLSC_INBUF_SIZE);
    c->plain = NULL;
    if (c->inbuf == NULL) {
        _snprintf(err, errlen, "out of memory");
        tlsc_free_conn(c);
        return 0;
    }

    memset(&scred, 0, sizeof(scred));
    scred.dwVersion = SCHANNEL_CRED_VERSION;
    scred.grbitEnabledProtocols = SP_PROT_TLS1_2_CLIENT;
    scred.dwFlags = SCH_CRED_NO_DEFAULT_CREDS | SCH_CRED_MANUAL_CRED_VALIDATION | SCH_USE_STRONG_CRYPTO | SCH_CRED_NO_SERVERNAME_CHECK;

    ss = AcquireCredentialsHandleA(NULL, (SEC_CHAR *) UNISP_NAME_A, SECPKG_CRED_OUTBOUND, NULL, &scred, NULL, NULL, &c->cred, &ts);
    if (ss != SEC_E_OK) {
        _snprintf(err, errlen, "AcquireCredentialsHandle failed (0x%08lX)", (unsigned long) ss);
        tlsc_free_conn(c);
        return 0;
    }
    c->haveCred = 1;

    /* in the table from the start, so that tlsc_close() during the handshake finds it */
    tlsc_lock();
    for (i = 0; i < TLSC_MAX_CONN; i++) {
        if (g_tlsc_table[i] == NULL) {
            g_tlsc_table[i] = c;
            break;
        }
    }
    tlsc_unlock();
    if (i >= TLSC_MAX_CONN) {
        _snprintf(err, errlen, "too many TLS connections");
        tlsc_free_conn(c);
        return 0;
    }

    flags = ISC_REQ_SEQUENCE_DETECT | ISC_REQ_REPLAY_DETECT | ISC_REQ_CONFIDENTIALITY | ISC_RET_EXTENDED_ERROR |
            ISC_REQ_ALLOCATE_MEMORY | ISC_REQ_STREAM | ISC_REQ_MANUAL_CRED_VALIDATION;

    for (;;) {
        SecBuffer     inb[2];
        SecBuffer     outb[1];
        SecBufferDesc ind, outd;
        int           haveInput = !first;

        if (haveInput && c->inlen == 0) {
            /* need more data from the server */
            int n = recv(s, (char *) c->inbuf, TLSC_INBUF_SIZE, 0);
            if (n <= 0) {
                _snprintf(err, errlen, "connection closed during TLS handshake");
                goto done;
            }
            c->inlen = n;
        }

        outb[0].pvBuffer = NULL;
        outb[0].BufferType = SECBUFFER_TOKEN;
        outb[0].cbBuffer = 0;
        outd.ulVersion = SECBUFFER_VERSION;
        outd.cBuffers = 1;
        outd.pBuffers = outb;

        if (haveInput) {
            inb[0].pvBuffer = c->inbuf;
            inb[0].cbBuffer = c->inlen;
            inb[0].BufferType = SECBUFFER_TOKEN;
            inb[1].pvBuffer = NULL;
            inb[1].cbBuffer = 0;
            inb[1].BufferType = SECBUFFER_EMPTY;
            ind.ulVersion = SECBUFFER_VERSION;
            ind.cBuffers = 2;
            ind.pBuffers = inb;
        }

        ss = InitializeSecurityContextA(&c->cred, first ? NULL : &c->ctx, (SEC_CHAR *) host, flags, 0, 0,
                                        haveInput ? &ind : NULL, 0, &c->ctx, &outd, &attrs, &ts);
        if (ss == SEC_E_OK || ss == SEC_I_CONTINUE_NEEDED || (FAILED(ss) && (attrs & ISC_RET_EXTENDED_ERROR))) {
            c->haveCtx = 1;
        }
        first = 0;

        if (outb[0].pvBuffer != NULL) {
            int sent = 0;
            if (outb[0].cbBuffer > 0) {
                sent = tlsc_raw_send_all(s, (const unsigned char *) outb[0].pvBuffer, (int) outb[0].cbBuffer);
            }
            FreeContextBuffer(outb[0].pvBuffer);
            if (sent < 0) {
                _snprintf(err, errlen, "send failed during TLS handshake");
                goto done;
            }
        }

        if (ss == SEC_E_INCOMPLETE_MESSAGE) {
            int n;
            if (c->inlen >= TLSC_INBUF_SIZE) {
                _snprintf(err, errlen, "TLS handshake message too large");
                goto done;
            }
            n = recv(s, (char *) c->inbuf + c->inlen, TLSC_INBUF_SIZE - c->inlen, 0);
            if (n <= 0) {
                _snprintf(err, errlen, "connection closed during TLS handshake");
                goto done;
            }
            c->inlen += n;
            first = 0;
            continue;
        }

        if (ss == SEC_E_OK || ss == SEC_I_CONTINUE_NEEDED) {
            /* keep bytes that were not part of the handshake */
            if (haveInput) {
                if (inb[1].BufferType == SECBUFFER_EXTRA && inb[1].cbBuffer > 0) {
                    memmove(c->inbuf, c->inbuf + (c->inlen - inb[1].cbBuffer), inb[1].cbBuffer);
                    c->inlen = (int) inb[1].cbBuffer;
                }
                else {
                    c->inlen = 0;
                }
            }
            if (ss == SEC_E_OK) {
                break;
            }
            continue;
        }

        if (ss == SEC_I_INCOMPLETE_CREDENTIALS) {
            _snprintf(err, errlen, "server asks for a client certificate (not supported)");
            goto done;
        }

        _snprintf(err, errlen, "TLS handshake failed (0x%08lX)", (unsigned long) ss);
        goto done;
    }

    if (QueryContextAttributes(&c->ctx, SECPKG_ATTR_STREAM_SIZES, &c->sizes) != SEC_E_OK) {
        _snprintf(err, errlen, "cannot get the TLS record sizes");
        goto done;
    }

    /* certificate check */
    if (QueryContextAttributes(&c->ctx, SECPKG_ATTR_REMOTE_CERT_CONTEXT, &cert) != SEC_E_OK || cert == NULL) {
        _snprintf(err, errlen, "the server sent no certificate");
        goto done;
    }
    tlsc_fingerprint(cert, fp);
    if (fpOut) {
        strncpy(fpOut, fp, 64);
        fpOut[64] = '\000';
    }
    if (pinned != NULL && pinned[0] != '\000' && fp[0] != '\000' && _stricmp(pinned, fp) == 0) {
        certErr = 0;   /* the pinned (trusted) certificate */
    }
    else {
        certErr = tlsc_check_chain(cert, host);
        if (certErr != 0) {
            if (certErrOut) *certErrOut = certErr;
            if (trust != NULL && fp[0] != '\000' && trust(trustCtx, fp, certErr)) {
                certErr = 0;
            }
        }
    }
    if (certErr != 0) {
        _snprintf(err, errlen, "server certificate not trusted (0x%08lX)", certErr);
        goto done;
    }

    c->outcap = (int) (c->sizes.cbHeader + c->sizes.cbMaximumMessage + c->sizes.cbTrailer);
    c->outbuf = (unsigned char *) malloc(c->outcap);
    c->plain = (unsigned char *) malloc(c->sizes.cbMaximumMessage > 0 ? c->sizes.cbMaximumMessage : 16384);
    if (c->outbuf == NULL || c->plain == NULL) {
        _snprintf(err, errlen, "out of memory");
        goto done;
    }

    ok = 1;

done:
    if (cert != NULL) {
        CertFreeCertificateContext(cert);
    }
    tlsc_lock();
    if (ok && c->dead) {
        ok = 0;
        _snprintf(err, errlen, "disconnected during TLS handshake");
    }
    if (ok) {
        c->ready = 1;
    }
    else if (!c->dead) {
        /* take it out of the table (tlsc_close() has not done so) */
        for (i = 0; i < TLSC_MAX_CONN; i++) {
            if (g_tlsc_table[i] == c) {
                g_tlsc_table[i] = NULL;
                c->dead = 1;
                c->refs--;
                break;
            }
        }
    }
    tlsc_unlock();
    tlsc_release(c);
    return ok;
}

/* send(): the data is encrypted in records. Plain sockets pass through. Returns len or -1. */
static int tlsc_send(SOCKET s, const char *data, int len, int flags)
{
    tlsc_conn *c = tlsc_acquire(s);
    int        done = 0;

    if (c == NULL) {
        return send(s, data, len, flags);
    }
    EnterCriticalSection(&c->cs);
    if (!c->ready) {
        done = -1;
    }
    while (done >= 0 && done < len && !c->dead) {
        SecBuffer     b[4];
        SecBufferDesc d;
        int           chunk = len - done;
        int           total;

        if (chunk > (int) c->sizes.cbMaximumMessage) {
            chunk = (int) c->sizes.cbMaximumMessage;
        }
        memcpy(c->outbuf + c->sizes.cbHeader, data + done, chunk);
        b[0].pvBuffer = c->outbuf;
        b[0].cbBuffer = c->sizes.cbHeader;
        b[0].BufferType = SECBUFFER_STREAM_HEADER;
        b[1].pvBuffer = c->outbuf + c->sizes.cbHeader;
        b[1].cbBuffer = chunk;
        b[1].BufferType = SECBUFFER_DATA;
        b[2].pvBuffer = c->outbuf + c->sizes.cbHeader + chunk;
        b[2].cbBuffer = c->sizes.cbTrailer;
        b[2].BufferType = SECBUFFER_STREAM_TRAILER;
        b[3].pvBuffer = NULL;
        b[3].cbBuffer = 0;
        b[3].BufferType = SECBUFFER_EMPTY;
        d.ulVersion = SECBUFFER_VERSION;
        d.cBuffers = 4;
        d.pBuffers = b;
        if (EncryptMessage(&c->ctx, 0, &d, 0) != SEC_E_OK) {
            done = -1;
            break;
        }
        total = (int) (b[0].cbBuffer + b[1].cbBuffer + b[2].cbBuffer);
        if (tlsc_raw_send_all(s, c->outbuf, total) < 0) {
            done = -1;
            break;
        }
        done += chunk;
    }
    if (c->dead && done < len) {
        done = -1;
    }
    LeaveCriticalSection(&c->cs);
    tlsc_release(c);
    return done;
}

/* recv(): returns the number of decrypted bytes, 0 when the server closed, -1 on error. */
static int tlsc_recv(SOCKET s, char *buf, int len, int flags)
{
    tlsc_conn *c = tlsc_acquire(s);
    int        result = -1;

    if (c == NULL) {
        return recv(s, buf, len, flags);
    }
    EnterCriticalSection(&c->cs);
    for (;;) {
        SecBuffer     b[4];
        SecBufferDesc d;
        SECURITY_STATUS ss;
        int           i;

        if (c->dead || !c->ready) {
            result = -1;
            break;
        }
        if (c->plainoff < c->plainlen) {
            int n = c->plainlen - c->plainoff;
            if (n > len) n = len;
            memcpy(buf, c->plain + c->plainoff, n);
            c->plainoff += n;
            result = n;
            break;
        }
        if (c->inlen > 0) {
            b[0].pvBuffer = c->inbuf;
            b[0].cbBuffer = c->inlen;
            b[0].BufferType = SECBUFFER_DATA;
            for (i = 1; i < 4; i++) {
                b[i].pvBuffer = NULL;
                b[i].cbBuffer = 0;
                b[i].BufferType = SECBUFFER_EMPTY;
            }
            d.ulVersion = SECBUFFER_VERSION;
            d.cBuffers = 4;
            d.pBuffers = b;
            ss = DecryptMessage(&c->ctx, &d, 0, NULL);
            if (ss == SEC_E_OK) {
                unsigned char *dataPtr = NULL;
                int            dataLen = 0;
                int            extraLen = 0;

                for (i = 1; i < 4; i++) {
                    if (b[i].BufferType == SECBUFFER_DATA && dataPtr == NULL) {
                        dataPtr = (unsigned char *) b[i].pvBuffer;
                        dataLen = (int) b[i].cbBuffer;
                    }
                    if (b[i].BufferType == SECBUFFER_EXTRA) {
                        extraLen = (int) b[i].cbBuffer;
                    }
                }
                if (dataPtr != NULL && dataLen > 0) {
                    if (dataLen > (int) c->sizes.cbMaximumMessage) {
                        dataLen = (int) c->sizes.cbMaximumMessage;
                    }
                    memcpy(c->plain, dataPtr, dataLen);
                    c->plainlen = dataLen;
                    c->plainoff = 0;
                }
                if (extraLen > 0) {
                    memmove(c->inbuf, c->inbuf + (c->inlen - extraLen), extraLen);
                    c->inlen = extraLen;
                }
                else {
                    c->inlen = 0;
                }
                continue;
            }
            if (ss == SEC_I_CONTEXT_EXPIRED) {
                result = 0;
                break;
            }
            if (ss != SEC_E_INCOMPLETE_MESSAGE) {
                result = -1;
                break;
            }
        }
        if (c->inlen >= TLSC_INBUF_SIZE) {
            result = -1;
            break;
        }
        {
            int n = recv(s, (char *) c->inbuf + c->inlen, TLSC_INBUF_SIZE - c->inlen, 0);
            if (n <= 0) {
                result = n;
                break;
            }
            c->inlen += n;
        }
    }
    LeaveCriticalSection(&c->cs);
    tlsc_release(c);
    return result;
}

/* TLS shutdown (when the socket is TLS wrapped) and closesocket() */
static int tlsc_close(SOCKET s)
{
    tlsc_conn *c = NULL;
    int        i;
    int        result;
    int        ready = 0;

    tlsc_lock();
    for (i = 0; i < TLSC_MAX_CONN; i++) {
        if (g_tlsc_table[i] != NULL && g_tlsc_table[i]->sock == s && !g_tlsc_table[i]->dead) {
            c = g_tlsc_table[i];   /* the reference of the table is now ours */
            c->dead = 1;
            ready = c->ready;
            break;
        }
    }
    tlsc_unlock();

    if (c != NULL) {
        if (ready && TryEnterCriticalSection(&c->cs)) {
            /* best effort close_notify */
            DWORD         type = SCHANNEL_SHUTDOWN;
            SecBuffer     ob;
            SecBufferDesc od;
            DWORD         attrs;
            TimeStamp     ts;
            SecBuffer     tb;

            tb.pvBuffer = &type;
            tb.cbBuffer = sizeof(type);
            tb.BufferType = SECBUFFER_TOKEN;
            od.ulVersion = SECBUFFER_VERSION;
            od.cBuffers = 1;
            od.pBuffers = &tb;
            if (ApplyControlToken(&c->ctx, &od) == SEC_E_OK) {
                SECURITY_STATUS ss;

                ob.pvBuffer = NULL;
                ob.cbBuffer = 0;
                ob.BufferType = SECBUFFER_TOKEN;
                od.pBuffers = &ob;
                ss = InitializeSecurityContextA(&c->cred, &c->ctx, NULL,
                                                ISC_REQ_SEQUENCE_DETECT | ISC_REQ_REPLAY_DETECT | ISC_REQ_CONFIDENTIALITY |
                                                ISC_RET_EXTENDED_ERROR | ISC_REQ_ALLOCATE_MEMORY | ISC_REQ_STREAM,
                                                0, 0, NULL, 0, &c->ctx, &od, &attrs, &ts);
                if ((ss == SEC_E_OK || ss == SEC_I_CONTEXT_EXPIRED) && ob.pvBuffer != NULL && ob.cbBuffer > 0) {
                    tlsc_raw_send_all(s, (const unsigned char *) ob.pvBuffer, (int) ob.cbBuffer);
                }
                if (ob.pvBuffer != NULL) {
                    FreeContextBuffer(ob.pvBuffer);
                }
            }
            LeaveCriticalSection(&c->cs);
        }
        else {
            shutdown(s, 2);   /* wake up a call that is still running */
        }
    }
    result = closesocket(s);
    if (c != NULL) {
        /* removed only now: until here tlsc_send() must not treat the socket as plain */
        tlsc_lock();
        for (i = 0; i < TLSC_MAX_CONN; i++) {
            if (g_tlsc_table[i] == c) {
                g_tlsc_table[i] = NULL;
                break;
            }
        }
        tlsc_unlock();
        tlsc_release(c);
    }
    return result;
}

#endif /* TLSCLIENT_H_INCLUDED */
'''


TLS_CPP = '/* shuicast_tls: tls: TLS for the server connection (Windows SChannel, see tlsclient.h) */\nstatic const WCHAR *acTlsReason(unsigned long e) {\n\tswitch (e) {\n\t\tcase 0x800B0109UL:\n\t\tcase 0x800B0112UL:\n\t\t\treturn L"\\u81ea\\u5df1\\u7f72\\u540d\\u3001\\u307e\\u305f\\u306f\\u4fe1\\u983c\\u3055\\u308c\\u3066\\u3044\\u306a\\u3044\\u767a\\u884c\\u5143\\u306e\\u8a3c\\u660e\\u66f8\\u3067\\u3059";\n\t\tcase 0x800B010FUL:\n\t\t\treturn L"\\u30db\\u30b9\\u30c8\\u540d\\u304c\\u8a3c\\u660e\\u66f8\\u3068\\u4e00\\u81f4\\u3057\\u307e\\u305b\\u3093";\n\t\tcase 0x800B0101UL:\n\t\t\treturn L"\\u8a3c\\u660e\\u66f8\\u306e\\u671f\\u9650\\u304c\\u5207\\u308c\\u3066\\u3044\\u307e\\u3059";\n\t}\n\treturn L"\\u8a3c\\u660e\\u66f8\\u3092\\u691c\\u8a3c\\u3067\\u304d\\u307e\\u305b\\u3093";\n}\n\nstatic int acTlsTrust(void *ctx, const char *fp, unsigned long certErr) {\n\tCEncoder\t*g = (CEncoder *) ctx;\n\tWCHAR\tmsg[1500];\n\tWCHAR\twfp[100];\n\tWCHAR\twhost[300];\n\tint\t\tanswer;\n\n\tif (g->m_TLSPinned[0] != \'\\000\') {\n\t\tg->LogMessage( LOG_ERROR, "TLS: the server certificate (SHA-256 %s) is not the trusted one. To trust a new certificate, delete ServerTLSTrustedCert in the config file.", fp);\n\t\treturn 0;\n\t}\n\tif (strcmp(g->m_TLSDeclined, fp) == 0) {\n\t\treturn 0;\n\t}\n\tMultiByteToWideChar(CP_ACP, 0, fp, -1, wfp, 99);\n\twfp[99] = 0;\n\tMultiByteToWideChar(CP_ACP, 0, g->m_Server, -1, whost, 299);\n\twhost[299] = 0;\n\t_snwprintf(msg, 1499, L"TLS\\u3067\\u63a5\\u7d9a\\u3059\\u308b\\u30b5\\u30fc\\u30d0\\u30fc\\u306e\\u8a3c\\u660e\\u66f8\\u3092\\u78ba\\u8a8d\\u3067\\u304d\\u307e\\u305b\\u3093\\u3067\\u3057\\u305f\\u3002\\n\\n\\u30b5\\u30fc\\u30d0\\u30fc: %ls\\n\\u7406\\u7531: %ls\\nSHA-256\\u6307\\u7d0b:\\n%ls\\n\\n\\u3053\\u306e\\u8a3c\\u660e\\u66f8\\u3092\\u4fe1\\u983c\\u3057\\u3066\\u63a5\\u7d9a\\u3057\\u3001\\u4ee5\\u5f8c\\u3053\\u306e\\u8a3c\\u660e\\u66f8\\u3060\\u3051\\u3092\\u8a31\\u53ef\\u3057\\u307e\\u3059\\u304b\\uff1f\\n\\u5225\\u306e\\u30b5\\u30fc\\u30d0\\u30fc\\u306b\\u3088\\u308b\\u507d\\u306e\\u8a3c\\u660e\\u66f8\\u306e\\u53ef\\u80fd\\u6027\\u3082\\u3042\\u308a\\u307e\\u3059\\u3002\\u6307\\u7d0b\\u304c\\u6b63\\u3057\\u3044\\u3068\\u78ba\\u8a8d\\u3067\\u304d\\u308b\\u5834\\u5408\\u306e\\u307f\\u300c\\u306f\\u3044\\u300d\\u3092\\u9078\\u3093\\u3067\\u304f\\u3060\\u3055\\u3044\\u3002", whost, acTlsReason(certErr), wfp);\n\tmsg[1499] = 0;\n\tanswer = MessageBoxW(NULL, msg, L"\\u30b5\\u30fc\\u30d0\\u30fc\\u8a3c\\u660e\\u66f8\\u306e\\u78ba\\u8a8d", MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST | MB_SETFOREGROUND);\n\tif (answer == IDYES) {\n\t\tstrncpy(g->m_TLSPinned, fp, sizeof(g->m_TLSPinned) - 1);\n\t\tg->m_TLSPinned[sizeof(g->m_TLSPinned) - 1] = \'\\000\';\n\t\tg->WriteConfigFile();\n\t\treturn 1;\n\t}\n\tstrncpy(g->m_TLSDeclined, fp, sizeof(g->m_TLSDeclined) - 1);\n\tg->m_TLSDeclined[sizeof(g->m_TLSDeclined) - 1] = \'\\000\';\n\treturn 0;\n}\n\nstatic int acTlsAttach(CEncoder *g, SOCKET s, int report) {\n\tchar\t\t\tfp[80] = "";\n\tchar\t\t\terr[256] = "";\n\tunsigned long\tcertErr = 0;\n\n\tif (tlsc_attach(s, g->m_Server, g->m_TLSPinned, acTlsTrust, g, fp, &certErr, err, sizeof(err))) {\n\t\treturn 1;\n\t}\n\tg->LogMessage( LOG_ERROR, "TLS: %s", err);\n\tif (report) {\n\t\tg->SetServerStatus( "TLS failed (see log)" );\n\t}\n\treturn 0;\n}\n\n'


def write_tlsclient(rel):
    path = SRC / rel
    if not path.parent.exists():
        fail("%s: folder not found" % rel)
    data = TLSCLIENT_H.encode("latin-1")
    if path.exists() and path.read_bytes() == data:
        return
    path.write_bytes(data)
    print(TAG + ": wrote " + rel)


def fix_tls_resource(t):
    if "IDC_USE_TLS" in t:
        return t
    return sub_once(
        t,
        r"(\n)(#ifdef APSTUDIO_INVOKED)",
        lambda m: m.group(1) + "#define IDC_USE_TLS                     1190\n\n" + m.group(2),
        "resource.h",
    )


def fix_tls_rc(t):
    if "IDC_USE_TLS" in t:
        return t
    return sub_once(
        t,
        r"([ \t]*EDITTEXT[ \t]+IDC_SERVER_PORT,[^\n]*\n)",
        lambda m: m.group(1) + '    CONTROL         "Use TLS (HTTPS)",IDC_USE_TLS,"Button",BS_AUTOCHECKBOX | WS_TABSTOP,160,133,110,10\n',
        "rc server port",
    )


def fix_tls_bs_header(t):
    if "m_UseTLS" in t:
        return t
    return sub_once(
        t,
        r"([ \t]*)BOOL[ \t]+m_UseBitrate;\n",
        lambda m: m.group(0) + m.group(1) + "BOOL      m_UseTLS;\n",
        "BasicSettings.h",
    )


def fix_tls_bs_cpp(t):
    if "m_UseTLS" in t:
        return t
    t = sub_once(
        t,
        r"([ \t]*)m_UseBitrate = FALSE;\n",
        lambda m: m.group(0) + m.group(1) + "m_UseTLS = FALSE;\n",
        "BasicSettings.cpp ctor",
    )
    t = sub_once(
        t,
        r"([ \t]*)DDX_Check\(pDX, IDC_USEBITRATE, m_UseBitrate\);\n",
        lambda m: m.group(0) + m.group(1) + "DDX_Check(pDX, IDC_USE_TLS, m_UseTLS);\n",
        "BasicSettings.cpp DDX",
    )
    return t


def fix_tls_config(t):
    if "m_UseTLS" in t:
        return t
    t = sub_once(
        t,
        r"([ \t]*)basicSettings->m_ServerPort = encoder->m_Port;\n",
        lambda m: m.group(0) + m.group(1) + "basicSettings->m_UseTLS = encoder->m_UseTLS ? TRUE : FALSE;\n",
        "Config.cpp globals to dialog",
    )
    t = sub_once(
        t,
        r"([ \t]*)strcpy\( encoder->m_Port,[ \t]*LPCSTR\( basicSettings->m_ServerPort \) \);\n",
        lambda m: m.group(0) + m.group(1) + "encoder->m_UseTLS = basicSettings->m_UseTLS ? 1 : 0;\n",
        "Config.cpp dialog to globals",
    )
    return t


def _tls_members(m):
    # the members are public; the access of the code that follows is restored
    labels = re.findall(r"^[ \t]*(public|private|protected):", m.string[: m.start()], re.M)
    prev = labels[-1] if labels else "private"
    ind = m.group(1)
    return (
        ind + "public:   // " + MARK_TLS + "\n"
        + ind + "int                   m_UseTLS                  = 0;\n"
        + ind + 'char                  m_TLSPinned[80]           = "";\n'
        + ind + 'char                  m_TLSDeclined[80]         = "";\n'
        + ind + prev + ":\n"
    )


def fix_tls_libheader(t):
    if MARK_TLS in t:
        return t
    return sub_once(
        t,
        r"([ \t]*)int[ \t]+m_SkipCloseWarning[ \t]*= 0;\n",
        lambda m: m.group(0) + _tls_members(m),
        "libheader TLS members",
    )


def fix_tls_lib(t):
    if MARK_TLS in t:
        return t
    t = sub_once(
        t,
        r"#include \"libshuicast_socket.h\"\n",
        lambda m: m.group(0) + '#include "tlsclient.h"   // ' + MARK_TLS + '\n',
        "lib include",
    )
    t = sub_once(
        t,
        r"// this needs to be virtualized to support NSV[^\n]*\n",
        lambda m: TLS_CPP + m.group(0),
        "lib TLS block",
    )
    t, n = re.subn(r"ret = send\( sd, data, length, sendflags \);", "ret = tlsc_send( sd, data, length, sendflags );", t)
    if n != 1:
        fail("lib send: expected 1, found %d" % n)
    t, n = re.subn(r"\brecv\( m_SCSocket,", "tlsc_recv( m_SCSocket,", t)
    if n != 2:
        fail("lib recv: expected 2, found %d" % n)
    t, n = re.subn(r"closesocket\( m_SCSocket(Ctrl)? \)", lambda m: "tlsc_close( m_SCSocket" + (m.group(1) or "") + " )", t)
    if n != 5:
        fail("lib closesocket: expected 5, found %d" % n)
    # the title update uses its own socket variable: updates running at the same time, or a
    # disconnect, must not close (or lose) each other's socket
    t = sub_once(
        t,
        r"m_SCSocketCtrl = (m_CtrlChannel\.DoConnect\([^;]*\);)\n"
        r"([ \t]*)if \( m_SCSocketCtrl != -1 \)"
        r"((?:\n[^\n]*){0,3}?\n[ \t]*)send\( m_SCSocketCtrl, contentString, strlen\( contentString \), 0 \);"
        r"((?:\n[^\n]*){0,3}?\n[ \t]*)tlsc_close\( m_SCSocketCtrl \);",
        lambda m: "int ctrlSocket = " + m.group(1) + "\n"
        + m.group(2) + "if ( ctrlSocket != -1 )"
        + m.group(3) + "if ( !m_UseTLS || acTlsAttach( this, ctrlSocket, 0 ) )\n"
        + "                    {\n"
        + "                        tlsc_send( ctrlSocket, contentString, strlen( contentString ), 0 );\n"
        + "                    }"
        + m.group(4) + "tlsc_close( ctrlSocket );",
        "lib control socket send",
    )
    t = sub_once(
        t,
        r"[ \t]*SetServerStatus\( \"Socket connected\" \);\n",
        lambda m: m.group(0) + "    // " + MARK_TLS + "\n    if ( m_UseTLS && !acTlsAttach( this, m_SCSocket, 1 ) )\n    {\n        tlsc_close( m_SCSocket );\n        return 0;\n    }\n",
        "lib connect",
    )
    # forget a socket once it is closed: a later disconnect must not close the same number
    # again (Windows may have given it to another connection by then)
    t = sub_n(
        t,
        r"\n([ \t]*)tlsc_close\( m_SCSocket \);\n(?=[ \t]*return 0;)",
        lambda m: m.group(0) + m.group(1) + "m_SCSocket = 0;\n",
        "lib forget closed socket",
        count=3,
    )
    t = sub_once(
        t,
        r"([ \t]*)GetConfigVariable\( gAppName, \"Port\", \"8000\", m_Port, sizeof\( m_Port \), NULL \);\n",
        lambda m: m.group(0) + "    m_UseTLS = (int) GetConfigVariable( gAppName, \"ServerTLS\", 0L, (char_t *) NULL );\n    GetConfigVariable( gAppName, \"ServerTLSTrustedCert\", \"\", m_TLSPinned, sizeof( m_TLSPinned ), NULL );\n",
        "lib config load",
    )
    t = sub_once(
        t,
        r"([ \t]*)PutConfigVariable\( gAppName, \"Port\", m_Port \);\n",
        lambda m: m.group(0) + "    PutConfigVariable( gAppName, \"ServerTLS\", (long) m_UseTLS );\n    PutConfigVariable( gAppName, \"ServerTLSTrustedCert\", m_TLSPinned );\n",
        "lib config store",
    )
    t = sub_once(
        t,
        r"([ \t]*)AddConfigVariable\( \"Port\" \);\n",
        lambda m: m.group(0) + "    AddConfigVariable( \"ServerTLS\" );\n    AddConfigVariable( \"ServerTLSTrustedCert\" );\n",
        "lib config add",
    )
    return t


# ----------------------------------------------------------------------
# apply
# ----------------------------------------------------------------------

patch_file("shuicast/Config.cpp", fix_tls_config)
patch_file("shuicast/BasicSettings.h", fix_tls_bs_header)
patch_file("shuicast/BasicSettings.cpp", fix_tls_bs_cpp)
patch_file("libshuicast/libshuicast.cpp", fix_tls_lib)
patch_file("shuicast/shuicast.rc", fix_tls_rc)
patch_file("libshuicast/libshuicast.h", fix_tls_libheader)
patch_file("shuicast/resource.h", fix_tls_resource)
write_tlsclient("libshuicast/tlsclient.h")
print(TAG + ": done")
