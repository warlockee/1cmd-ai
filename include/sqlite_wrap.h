/*
 * sqlite_wrap.h - SQLite row/column wrapper types for onecmd
 *
 * Calling spec:
 *   Inputs:  None (pure declarations)
 *   Outputs: None
 *   Side effects: None — header-only, no code generation
 *
 * Provides sqlCol and sqlRow structures used by the SQL query helpers
 * in bot.h (sqlSelect, sqlSelectOneRow, sqlNextRow, sqlEnd, etc.).
 */

#ifndef ONECMD_SQLITE_WRAP_H
#define ONECMD_SQLITE_WRAP_H

#include <stdint.h>
#include <sqlite3.h>

#define SQL_MAX_SPEC 32     /* Maximum number of ?... specifiers per query. */

typedef struct sqlCol {
    int type;
    int64_t i;          /* Integer or len of string/blob. */
    const char *s;      /* String or blob. */
    double d;           /* Double. */
} sqlCol;

typedef struct sqlRow {
    sqlite3_stmt *stmt; /* Handle for this query. */
    int cols;           /* Number of columns. */
    sqlCol *col;        /* Array of columns. Note that the first time this
                           will be NULL, so we know we don't need to call
                           sqlite3_step() since it was called by the
                           query function. */
} sqlRow;

#endif /* ONECMD_SQLITE_WRAP_H */
