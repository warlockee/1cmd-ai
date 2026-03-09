/*
 * manager_ipc.c - Manager subprocess IPC for onecmd
 *
 * Contains mgr_reader_thread(), mgr_start(), mgr_send().
 * Manages the child process lifecycle and pipe-based communication
 * with the LLM manager agent (onecmd-mgr).
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <pthread.h>
#include <errno.h>

#include "types.h"
#include "bot.h"
#include "manager_ipc.h"
#include "format.h"
#include "sds.h"
#include "cJSON.h"
#include "state.h"

/* ============================================================================
 * LLM Manager Agent (onecmd-mgr)
 * ========================================================================= */

/* Reader thread: reads JSON lines from mgr stdout, forwards to Telegram. */
void *mgr_reader_thread(void *arg) {
    UNUSED(arg);
    static int restart_count = 0;
    static time_t last_restart = 0;

    FILE *fp = fdopen(MgrReadFd, "r");
    if (!fp) return NULL;

    /* Manager stayed up — reset crash counter. */
    if (time(NULL) - last_restart > 60) restart_count = 0;

    char line[8192];
    while (fgets(line, sizeof(line), fp)) {
        /* Parse JSON: {"chat_id": N, "text": "..."} */
        cJSON *msg = cJSON_Parse(line);
        if (!msg) continue;

        cJSON *chat = cJSON_Select(msg, ".chat_id:n");
        cJSON *text = cJSON_Select(msg, ".text:s");

        if (chat && text) {
            int64_t chat_id = (int64_t)chat->valuedouble;
            sds escaped = markdown_escape(text->valuestring);
            botSendMessage(chat_id, escaped, 0);
            sdsfree(escaped);
        }
        cJSON_Delete(msg);
    }

    fclose(fp);

    /* Mgr process ended -- clean up and auto-restart. */
    pthread_mutex_lock(&MgrLock);
    MgrPid = -1;
    MgrWriteFd = -1;
    MgrReadFd = -1;
    MgrReaderRunning = 0;
    pthread_mutex_unlock(&MgrLock);

    if (!ShutdownRequested) {
        restart_count++;
        if (restart_count > 5) {
            printf("MGR: Too many crashes (%d). Giving up.\n", restart_count);
            return NULL;
        }
        int delay = restart_count * 3; /* 3s, 6s, 9s, 12s, 15s */
        printf("MGR: Manager died. Restart %d/5 in %ds...\n", restart_count, delay);
        sleep(delay);
        last_restart = time(NULL);
        if (!ShutdownRequested && mgr_start() == 0) {
            printf("MGR: Manager auto-restarted.\n");
        } else if (!ShutdownRequested) {
            printf("MGR: Auto-restart failed.\n");
        }
    }
    return NULL;
}

/* Start the mgr child process. Returns 0 on success. */
int mgr_start(void) {
    if (MgrPid > 0) return 0; /* Already running. */

    if (MgrPath[0] == '\0') {
        fprintf(stderr, "MGR: No manager path configured.\n");
        return -1;
    }

    int pipe_to_mgr[2];   /* [0]=mgr reads, [1]=onecmd writes */
    int pipe_from_mgr[2]; /* [0]=onecmd reads, [1]=mgr writes */

    if (pipe(pipe_to_mgr) < 0) {
        perror("MGR: pipe(to_mgr)");
        return -1;
    }
    if (pipe(pipe_from_mgr) < 0) {
        perror("MGR: pipe(from_mgr)");
        close(pipe_to_mgr[0]);
        close(pipe_to_mgr[1]);
        return -1;
    }

    pid_t pid = fork();
    if (pid < 0) {
        perror("MGR: fork");
        return -1;
    }

    if (pid == 0) {
        /* Child: mgr process. */
        close(pipe_to_mgr[1]);
        close(pipe_from_mgr[0]);
        dup2(pipe_to_mgr[0], STDIN_FILENO);
        dup2(pipe_from_mgr[1], STDOUT_FILENO);
        close(pipe_to_mgr[0]);
        close(pipe_from_mgr[1]);

        /* Determine ctl path (same dir as onecmd binary). */
        char ctl_path[512];
        char mgr_dir[512];
        char *slash = strrchr(MgrPath, '/');
        if (slash) {
            size_t dirlen = slash - MgrPath;
            snprintf(mgr_dir, sizeof(mgr_dir), "%.*s", (int)dirlen, MgrPath);
        } else {
            snprintf(mgr_dir, sizeof(mgr_dir), ".");
        }
        snprintf(ctl_path, sizeof(ctl_path), "./onecmd-ctl");
        setenv("ONECMD_CTL", ctl_path, 1);

        /* Try venv python first, fall back to system python3. */
        char venv_py[512];
        snprintf(venv_py, sizeof(venv_py), "%s/.venv/bin/python3", mgr_dir);
        if (access(venv_py, X_OK) == 0) {
            execl(venv_py, "python3", MgrPath, (char *)NULL);
        }
        execlp("python3", "python3", MgrPath, (char *)NULL);
        perror("MGR: exec");
        _exit(1);
    }

    /* Parent: onecmd. */
    close(pipe_to_mgr[0]);
    close(pipe_from_mgr[1]);
    MgrWriteFd = pipe_to_mgr[1];
    MgrReadFd = pipe_from_mgr[0];
    MgrPid = pid;

    /* Start reader thread. */
    if (pthread_create(&MgrReaderThread, NULL, mgr_reader_thread, NULL) == 0) {
        MgrReaderRunning = 1;
    }

    printf("MGR: Started manager (pid %d).\n", (int)MgrPid);
    return 0;
}

/* Send a message to the mgr process. Attempts restart on failure. */
int mgr_send(int64_t chat_id, const char *text) {
    /* Lock MgrLock to prevent race with reader thread cleanup. */
    pthread_mutex_lock(&MgrLock);

    /* If mgr is not running, try to start it. */
    if (MgrWriteFd < 0 || MgrPid <= 0) {
        if (mgr_start() != 0) {
            pthread_mutex_unlock(&MgrLock);
            return -1;
        }
    }

    int write_fd = MgrWriteFd;  /* Snapshot fd under lock. */
    pthread_mutex_unlock(&MgrLock);

    cJSON *msg = cJSON_CreateObject();
    cJSON_AddNumberToObject(msg, "chat_id", (double)chat_id);
    cJSON_AddStringToObject(msg, "text", text);
    char *json = cJSON_PrintUnformatted(msg);
    cJSON_Delete(msg);

    sds line = sdscatprintf(sdsempty(), "%s\n", json);
    free(json);

    ssize_t written = write(write_fd, line, sdslen(line));

    if (written < 0) {
        perror("MGR: write failed, restarting");
        pthread_mutex_lock(&MgrLock);
        close(MgrWriteFd);
        MgrWriteFd = -1;
        MgrPid = -1;
        pthread_mutex_unlock(&MgrLock);

        /* Try one restart. */
        if (mgr_start() == 0) {
            written = write(MgrWriteFd, line, sdslen(line));
        }
    }

    sdsfree(line);
    return (written > 0) ? 0 : -1;
}
