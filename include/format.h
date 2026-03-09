/*
 * format.h - Message formatting for onecmd
 *
 * Calling spec:
 *   Pure text transformation functions. No side effects except allocation.
 *   - markdown_escape(s) -> sds (caller frees)
 *   - html_escape(text) -> sds (caller frees)
 *   - build_list_message() -> sds (caller frees)
 *   - build_help_message() -> sds (caller frees)
 *   - get_visible_lines() -> int
 *   - get_split_messages() -> int (0 or 1)
 *   - last_n_lines(text, n) -> const char* (pointer into text)
 */

#ifndef ONECMD_FORMAT_H
#define ONECMD_FORMAT_H

#include "sds.h"

/* Escape Markdown special characters for Telegram Markdown parse mode. */
sds markdown_escape(const char *s);

/* Escape text for Telegram HTML parse mode. */
sds html_escape(const char *text);

/* Build the .list response message. */
sds build_list_message(void);

/* Build the .help response message. */
sds build_help_message(void);

/* Get visible lines from ONECMD_VISIBLE_LINES env var (default 40). */
int get_visible_lines(void);

/* Check if multi-message splitting is enabled (default: off). */
int get_split_messages(void);

/* Get the last N lines from text. Returns pointer into the string. */
const char *last_n_lines(const char *text, int n);

#endif /* ONECMD_FORMAT_H */
