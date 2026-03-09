/*
 * totp.c - TOTP authentication for onecmd
 *
 * Contains base32 encoding, TOTP code computation, QR code display,
 * hex conversion, secret setup, and OTP verification.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>

#include "types.h"
#include "bot.h"
#include "totp.h"
#include "sqlite_wrap.h"
#include "sha1.h"
#include "qrcodegen.h"
#include "state.h"

/* ============================================================================
 * TOTP Authentication
 * ========================================================================= */

/* Encode raw bytes to Base32 string (RFC 4648). Returns static buffer. */
static const char *base32_encode(const unsigned char *data, size_t len) {
    static char out[128];
    static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
    int i = 0, j = 0;
    uint64_t buf = 0;
    int bits = 0;

    for (i = 0; i < (int)len; i++) {
        buf = (buf << 8) | data[i];
        bits += 8;
        while (bits >= 5) {
            bits -= 5;
            out[j++] = alphabet[(buf >> bits) & 0x1f];
        }
    }
    if (bits > 0) {
        out[j++] = alphabet[(buf << (5 - bits)) & 0x1f];
    }
    out[j] = '\0';
    return out;
}

/* Compute 6-digit TOTP code from raw secret and time step. */
static uint32_t totp_code(const unsigned char *secret, size_t secret_len,
                          uint64_t time_step)
{
    unsigned char msg[8];
    for (int i = 7; i >= 0; i--) {
        msg[i] = (unsigned char)(time_step & 0xff);
        time_step >>= 8;
    }

    unsigned char hash[SHA1_DIGEST_SIZE];
    hmac_sha1(secret, secret_len, msg, 8, hash);

    int offset = hash[19] & 0x0f;
    uint32_t code = ((uint32_t)(hash[offset] & 0x7f) << 24)
                  | ((uint32_t)hash[offset+1] << 16)
                  | ((uint32_t)hash[offset+2] << 8)
                  | (uint32_t)hash[offset+3];
    return code % 1000000;
}

/* Print QR code as compact ASCII art using half-block characters.
 * Each output line encodes two QR rows using upper/lower half blocks. */
static void print_qr_ascii(const char *text) {
    uint8_t qrcode[qrcodegen_BUFFER_LEN_MAX];
    uint8_t tempbuf[qrcodegen_BUFFER_LEN_MAX];

    if (!qrcodegen_encodeText(text, tempbuf, qrcode,
            qrcodegen_Ecc_LOW, qrcodegen_VERSION_MIN, qrcodegen_VERSION_MAX,
            qrcodegen_Mask_AUTO, true)) {
        printf("Failed to generate QR code.\n");
        return;
    }

    int size = qrcodegen_getSize(qrcode);
    int lo = -1, hi = size + 1; /* 1-module quiet zone. */

    for (int y = lo; y < hi; y += 2) {
        for (int x = lo; x < hi; x++) {
            int top = (x >= 0 && x < size && y >= 0 && y < size &&
                       qrcodegen_getModule(qrcode, x, y));
            int bot = (x >= 0 && x < size && y+1 >= 0 && y+1 < size &&
                       qrcodegen_getModule(qrcode, x, y+1));
            if (top && bot)       printf("\xe2\x96\x88"); /* full block */
            else if (top && !bot) printf("\xe2\x96\x80"); /* upper half */
            else if (!top && bot) printf("\xe2\x96\x84"); /* lower half */
            else                  printf(" ");
        }
        printf("\n");
    }
}

/* Convert hex string to raw bytes. Returns number of bytes written. */
static int hex_to_bytes(const char *hex, unsigned char *out, int max) {
    int len = 0;
    while (*hex && *(hex+1) && len < max) {
        unsigned int byte;
        if (sscanf(hex, "%2x", &byte) != 1) break;
        out[len++] = (unsigned char)byte;
        hex += 2;
    }
    return len;
}

/* Convert raw bytes to hex string. Returns static buffer. */
static const char *bytes_to_hex(const unsigned char *data, int len) {
    static char hex[128];
    for (int i = 0; i < len && i < 63; i++) {
        snprintf(hex + i*2, sizeof(hex) - (size_t)(i*2), "%02x", data[i]);
    }
    hex[len*2] = '\0';
    return hex;
}

/* Setup TOTP: check for existing secret, generate if needed, display QR.
 * The db_path is the SQLite database file path.
 * Returns the secret length in bytes, or 0 on error/weak-security. */
int totp_setup(const char *db_path) {
    if (WeakSecurity) return 0;

    sqlite3 *db;
    if (sqlite3_open(db_path, &db) != SQLITE_OK) {
        fprintf(stderr, "Cannot open database for TOTP setup.\n");
        return 0;
    }
    /* Ensure KV table exists. */
    sqlite3_exec(db, TB_CREATE_KV_STORE, 0, 0, NULL);

    /* OTP is off by default.  Only activate when --enable-otp is passed. */
    if (!EnableOtp) {
        WeakSecurity = 1;
        sqlite3_close(db);
        return 0;
    }

    /* Check for existing secret. */
    sds existing = kvGet(db, "totp_secret");
    if (existing) {
        sdsfree(existing);
        /* Load stored timeout if present. */
        sds timeout_str = kvGet(db, "otp_timeout");
        if (timeout_str) {
            int t = atoi(timeout_str);
            if (t >= 30 && t <= 28800) OtpTimeout = t;
            sdsfree(timeout_str);
        }
        sqlite3_close(db);
        return 1; /* Secret already exists. */
    }

    /* Generate 20 random bytes. */
    unsigned char secret[20];
    FILE *f = fopen("/dev/urandom", "r");
    if (!f || fread(secret, 1, 20, f) != 20) {
        fprintf(stderr, "Failed to read /dev/urandom, aborting: "
                        "can't proceed without TOTP secret generation.\n");
        exit(1);
    }
    fclose(f);

    /* Store as hex in KV. */
    kvSet(db, "totp_secret", bytes_to_hex(secret, 20), 0);
    sqlite3_close(db);

    /* Build otpauth URI and display QR code. */
    const char *b32 = base32_encode(secret, 20);
    char uri[256];
    snprintf(uri, sizeof(uri),
             "otpauth://totp/tgterm?secret=%s&issuer=tgterm", b32);

    printf("\n=== TOTP Setup ===\n");
    printf("Scan this QR code with Google Authenticator:\n\n");
    print_qr_ascii(uri);
    printf("\nOr enter this secret manually: %s\n", b32);
    printf("==================\n\n");
    fflush(stdout);

    return 1;
}

/* Check if the given code matches the current TOTP (with +/-1 window). */
int totp_verify(sqlite3 *db, const char *code_str) {
    sds hex = kvGet(db, "totp_secret");
    if (!hex) return 0;

    unsigned char secret[20];
    int slen = hex_to_bytes(hex, secret, 20);
    sdsfree(hex);
    if (slen != 20) return 0;

    uint64_t now = (uint64_t)time(NULL) / 30;
    uint32_t input_code = (uint32_t)atoi(code_str);

    for (int i = -1; i <= 1; i++) {
        if (totp_code(secret, 20, now + i) == input_code)
            return 1;
    }
    return 0;
}
