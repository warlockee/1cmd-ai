/*
 * totp.h - TOTP authentication for onecmd
 *
 * Calling spec:
 *   - totp_setup(db_path) -> int (1=secret ready, 0=error or weak security)
 *   - totp_verify(db, code_str) -> int (1=valid, 0=invalid)
 *   Side effects: SQLite reads/writes, stdout for QR display
 */

#ifndef ONECMD_TOTP_H
#define ONECMD_TOTP_H

#include <sqlite3.h>

/* Setup TOTP: check for existing secret, generate if needed, display QR.
 * Returns 1 if secret is ready, 0 on error or weak security. */
int totp_setup(const char *db_path);

/* Check if the given code matches the current TOTP (with +/-1 window).
 * Returns 1 on match, 0 on failure. */
int totp_verify(sqlite3 *db, const char *code_str);

#endif /* ONECMD_TOTP_H */
