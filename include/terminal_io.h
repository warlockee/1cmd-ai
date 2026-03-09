/*
 * terminal_io.h - Terminal text display for onecmd
 *
 * Calling spec:
 *   Terminal text capture/display pipeline.
 *   - send_terminal_text(chat_id) -> void, sends captured text to Telegram
 *   - send_html_message(target, text) -> int64_t message_id
 *   - delete_terminal_messages(chat_id) -> void
 *   - format_terminal_messages(raw, count) -> sds* (caller frees each + array)
 *   Side effects: Telegram messages
 */

#ifndef ONECMD_TERMINAL_IO_H
#define ONECMD_TERMINAL_IO_H

#include <stdint.h>
#include "sds.h"

/* Send terminal text with refresh button. Deletes previous tracked messages. */
void send_terminal_text(int64_t chat_id);

/* Send a plain HTML message (no inline keyboard). Returns message_id or 0. */
int64_t send_html_message(int64_t target, sds text);

/* Delete all tracked terminal messages, then reset tracking. */
void delete_terminal_messages(int64_t chat_id);

/* Format terminal text into one or more HTML <pre> messages.
 * Caller must sdsfree each element and xfree the array. */
sds *format_terminal_messages(sds raw, int *count);

#endif /* ONECMD_TERMINAL_IO_H */
