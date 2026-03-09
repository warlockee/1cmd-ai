/*
 * commands.h - Command handler interface for onecmd
 *
 * Calling spec:
 *   - handle_request(db, br) -> void, processes one BotRequest
 *   - cron_callback(db) -> void, periodic task
 *   - disconnect() -> void, resets connection state
 */

#ifndef ONECMD_COMMANDS_H
#define ONECMD_COMMANDS_H

#include "types.h"

/* Dispatch a user command from a Telegram message. */
void handle_request(sqlite3 *db, BotRequest *br);

/* Periodic callback (currently a no-op). */
void cron_callback(sqlite3 *db);

/* Disconnect from current terminal session. */
void disconnect(void);

#endif /* ONECMD_COMMANDS_H */
