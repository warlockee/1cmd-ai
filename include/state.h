/*
 * state.h - Shared mutable state for onecmd
 *
 * Calling spec:
 *   All variables are defined in main.c.
 *   Other modules use extern declarations via this header.
 *   Side effects: none (pure declarations).
 */

#ifndef ONECMD_STATE_H
#define ONECMD_STATE_H

#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <time.h>

/* Request serialization. */
extern pthread_mutex_t RequestLock;

/* Manager IPC lock (separate from RequestLock to avoid deadlock). */
extern pthread_mutex_t MgrLock;

/* Graceful shutdown flag. */
extern volatile sig_atomic_t ShutdownRequested;

/* Security / authentication state. */
extern int WeakSecurity;
extern int EnableOtp;
extern int Authenticated;
extern time_t LastActivity;
extern int OtpTimeout;

/* Terminal message tracking. */
#define MAX_TRACKED_MSGS 16
extern int64_t TrackedMsgIds[MAX_TRACKED_MSGS];
extern int TrackedMsgCount;

/* LLM Manager Agent state. */
extern int MgrMode;
extern pid_t MgrPid;
extern int MgrWriteFd;
extern int MgrReadFd;
extern pthread_t MgrReaderThread;
extern int MgrReaderRunning;
extern char MgrPath[512];

#endif /* ONECMD_STATE_H */
