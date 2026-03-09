UNAME_S := $(shell uname -s)

ifeq ($(UNAME_S),Darwin)
    CC = clang
    CFLAGS = -Wall -O2 -mmacosx-version-min=14.0
    FRAMEWORKS = -framework CoreGraphics -framework CoreFoundation \
                 -framework CoreServices -framework ApplicationServices
    BACKEND = backend_macos.o
else
    CC = gcc
    CFLAGS = -Wall -O2
    FRAMEWORKS =
    BACKEND = backend_tmux.o
endif

INCLUDES = -Iinclude -Ivendor
LIBS = -lcurl -lsqlite3

OBJS = main.o commands.o totp.o format.o terminal_display.o manager_ipc.o emoji.o \
       bot_http.o bot_api.o bot_poll.o bot_utils.o $(BACKEND) \
       sqlite_wrap.o json_wrap.o \
       cJSON.o sds.o qrcodegen.o sha1.o

CTL_OBJS = onecmd_ctl.o emoji.o $(BACKEND) sds.o cJSON.o

all: onecmd onecmd-ctl

onecmd: $(OBJS)
	$(CC) $(CFLAGS) -o $@ $(OBJS) $(FRAMEWORKS) $(LIBS)

onecmd-ctl: $(CTL_OBJS)
	$(CC) $(CFLAGS) -o $@ $(CTL_OBJS) $(FRAMEWORKS)

# --- src/ compilation rules ---

main.o: src/main.c include/bot.h include/types.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/main.c

commands.o: src/commands.c include/commands.h include/bot.h include/types.h include/totp.h include/format.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/commands.c

totp.o: src/totp.c include/totp.h vendor/sha1.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/totp.c

format.o: src/format.c include/format.h include/types.h vendor/cJSON.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/format.c

terminal_display.o: src/terminal_display.c include/terminal_io.h include/backend.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/terminal_display.c

manager_ipc.o: src/manager_ipc.c include/manager_ipc.h include/state.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/manager_ipc.c

emoji.o: src/emoji.c include/emoji.h include/backend.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/emoji.c

bot_http.o: src/bot_http.c include/bot.h include/types.h vendor/cJSON.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/bot_http.c

bot_utils.o: src/bot_utils.c include/bot_utils.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/bot_utils.c

bot_api.o: src/bot_api.c include/bot.h include/types.h vendor/cJSON.h vendor/sds.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/bot_api.c

bot_poll.o: src/bot_poll.c include/bot.h include/types.h include/commands.h vendor/cJSON.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/bot_poll.c

backend_macos.o: src/backend_macos.c include/backend.h vendor/sds.h include/bot.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/backend_macos.c

backend_tmux.o: src/backend_tmux.c include/backend.h vendor/sds.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/backend_tmux.c

sqlite_wrap.o: src/sqlite_wrap.c include/sqlite_wrap.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/sqlite_wrap.c

json_wrap.o: src/json_wrap.c vendor/cJSON.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/json_wrap.c

onecmd_ctl.o: src/onecmd_ctl.c include/backend.h vendor/sds.h vendor/cJSON.h
	$(CC) $(CFLAGS) $(INCLUDES) -c src/onecmd_ctl.c

# --- vendor/ compilation rules ---

cJSON.o: vendor/cJSON.c vendor/cJSON.h
	$(CC) $(CFLAGS) $(INCLUDES) -c vendor/cJSON.c

sds.o: vendor/sds.c vendor/sds.h vendor/sdsalloc.h
	$(CC) $(CFLAGS) $(INCLUDES) -c vendor/sds.c

qrcodegen.o: vendor/qrcodegen.c vendor/qrcodegen.h
	$(CC) $(CFLAGS) $(INCLUDES) -c vendor/qrcodegen.c

sha1.o: vendor/sha1.c vendor/sha1.h
	$(CC) $(CFLAGS) $(INCLUDES) -c vendor/sha1.c

clean:
	rm -f onecmd onecmd-ctl *.o

.PHONY: all clean
