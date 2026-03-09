/*
 * bot.h - Telegram Bot API wrapper for onecmd
 *
 * Calling spec:
 *   Inputs:  Telegram bot token (via startBot initialization)
 *   Outputs: HTTP responses, message IDs
 *   Side effects: Network I/O, SQLite reads/writes
 *
 * All functions require prior startBot() initialization.
 */

#ifndef ONECMD_BOT_H
#define ONECMD_BOT_H

#include "types.h"
#include "sds.h"
#include "cJSON.h"
#include "sqlite_wrap.h"
#include "bot_utils.h"

/* Bot global state (defined in bot_poll.c). */
typedef struct {
    int debug;
    int verbose;
    char *dbfile;
    char **triggers;
    sds apikey;
    sds username;
    TBRequestCallback req_callback;
    TBCronCallback cron_callback;
} OneCmdBot;

extern OneCmdBot Bot;

/* Thread-local database handle (defined in bot_poll.c). */
extern _Thread_local sqlite3 *DbHandle;

/* Database init/close. */
sqlite3 *dbInit(char *createdb_query);
void dbClose(void);

/* Bot stats. */
void resetBotStats(void);
void readApiKeyFromFile(void);
void botMain(void);

/* HTTP */
size_t makeHTTPGETCallWriterSDS(char *ptr, size_t size, size_t nmemb, void *userdata);
size_t makeHTTPGETCallWriterFILE(char *ptr, size_t size, size_t nmemb, void *userdata);
sds makeHTTPGETCallOpt(const char *url, int *resptr, char **optlist, int optnum);
sds makeHTTPGETCall(const char *url, int *resptr);

/* Telegram bot API. */
int startBot(char *createdb_query, int argc, char **argv, int flags,
             TBRequestCallback req_callback, TBCronCallback cron_callback,
             char **triggers);
sds makeGETBotRequest(const char *action, int *resptr, char **optlist, int numopt);
int botSendMessageAndGetInfo(int64_t target, sds text, int64_t reply_to,
                             int64_t *chat_id, int64_t *message_id);
int botSendMessage(int64_t target, sds text, int64_t reply_to);
void botSendMessageAsync(int64_t target, const char *text, int64_t reply_to);
int botEditMessageText(int64_t chat_id, int message_id, sds text);
int botSendMessageWithKeyboard(int64_t target, sds text, const char *parse_mode,
                               const char *btn_text, const char *btn_data,
                               int64_t *msg_id);
int botEditMessageTextWithKeyboard(int64_t chat_id, int64_t message_id, sds text,
                                   const char *parse_mode, const char *btn_text,
                                   const char *btn_data);
int botAnswerCallbackQuery(const char *callback_id);
int botGetFile(BotRequest *br, const char *target_filename);
char *botGetUsername(void);
void freeBotRequest(BotRequest *br);
BotRequest *createBotRequest(void);

/* Database. */
int kvSetLen(sqlite3 *dbhandle, const char *key, const char *value,
             size_t vlen, int64_t expire);
int kvSet(sqlite3 *dbhandle, const char *key, const char *value, int64_t expire);
sds kvGet(sqlite3 *dbhandle, const char *key);
void kvDel(sqlite3 *dbhandle, const char *key);
void sqlEnd(sqlRow *row);
int sqlNextRow(sqlRow *row);
int sqlInsert(sqlite3 *dbhandle, const char *sql, ...);
int sqlQuery(sqlite3 *dbhandle, const char *sql, ...);
int sqlSelect(sqlite3 *dbhandle, sqlRow *row, const char *sql, ...);
int sqlSelectOneRow(sqlite3 *dbhandle, sqlRow *row, const char *sql, ...);
int64_t sqlSelectInt(sqlite3 *dbhandle, const char *sql, ...);

/* Json */
cJSON *cJSON_Select(cJSON *o, const char *fmt, ...);

#endif /* ONECMD_BOT_H */
