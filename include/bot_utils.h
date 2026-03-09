/*
 * bot_utils.h - General utility functions for onecmd
 *
 * Calling spec:
 *   - strmatch(pattern, patternLen, string, stringLen, nocase) -> int (1=match, 0=no)
 *     Glob-style pattern matching with *, ?, [charset], and \ escaping.
 *   - xmalloc(size) -> void* (aborts on OOM)
 *   - xrealloc(ptr, size) -> void* (aborts on OOM)
 *   - xfree(ptr) -> void
 *   Side effects: xmalloc/xrealloc call exit(1) on allocation failure.
 */

#ifndef ONECMD_BOT_UTILS_H
#define ONECMD_BOT_UTILS_H

#include <stddef.h>

/* Glob-style pattern matching. Return 1 on match, 0 otherwise. */
int strmatch(const char *pattern, int patternLen,
             const char *string, int stringLen, int nocase);

/* Allocation wrappers that abort on OOM. */
void *xmalloc(size_t size);
void *xrealloc(void *ptr, size_t size);
void xfree(void *ptr);

#endif /* ONECMD_BOT_UTILS_H */
