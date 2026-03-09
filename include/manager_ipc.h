/*
 * manager_ipc.h - Manager subprocess IPC for onecmd
 *
 * Calling spec:
 *   LLM manager agent child process lifecycle and communication.
 *   - mgr_reader_thread(arg) -> void* (pthread entry point)
 *   - mgr_start() -> int (0=ok, -1=error)
 *   - mgr_send(chat_id, text) -> int (0=ok, -1=error)
 *   Side effects: pipe I/O, child process management, Telegram messages
 */

#ifndef ONECMD_MANAGER_IPC_H
#define ONECMD_MANAGER_IPC_H

#include <stdint.h>

/* Reader thread: reads JSON lines from mgr stdout, forwards to Telegram. */
void *mgr_reader_thread(void *arg);

/* Start the mgr child process. Returns 0 on success. */
int mgr_start(void);

/* Send a message to the mgr process. Attempts restart on failure. */
int mgr_send(int64_t chat_id, const char *text);

#endif /* ONECMD_MANAGER_IPC_H */
