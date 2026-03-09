/*
 * backend.h - Backend interface for onecmd
 *
 * Calling spec:
 *   Abstract interface for platform-specific terminal control.
 *   - backend_list() -> int (count), fills global TermList/TermCount
 *   - backend_free_list() -> void
 *   - backend_connected() -> int (1=alive, 0=dead)
 *   - backend_capture_text() -> sds (caller frees) or NULL
 *   - backend_send_keys(text) -> int (0=ok, -1=error)
 *
 * Implemented by backend_macos.c or backend_tmux.c.
 */

#ifndef ONECMD_BACKEND_H
#define ONECMD_BACKEND_H

#include "types.h"

/* List available terminal sessions. Fills TermList/TermCount. Returns count. */
int backend_list(void);

/* Free the terminal list. */
void backend_free_list(void);

/* Check if current connection is still alive. Returns 1 if yes. */
int backend_connected(void);

/* Capture visible text from connected terminal. Returns sds string or NULL. */
sds backend_capture_text(void);

/* Send keystrokes to connected terminal.
 * text: raw input with emoji modifiers.
 * Returns 0 on success, -1 on error. */
int backend_send_keys(const char *text);

#endif /* ONECMD_BACKEND_H */
