CC ?= clang
PKG_CONFIG ?= pkg-config
CFLAGS ?= -O2 -Wall -Wextra -std=c11

ifeq ($(HAS_WEBKIT),1)
WEBKIT_CFLAGS := $(shell $(PKG_CONFIG) --cflags gtk+-3.0 webkit2gtk-4.1)
WEBKIT_LIBS := $(shell $(PKG_CONFIG) --libs gtk+-3.0 webkit2gtk-4.1)
CFLAGS += -DFOLIO_HAS_WEBKITGTK $(WEBKIT_CFLAGS)
LDFLAGS += $(WEBKIT_LIBS)
endif

folio: main.c
	$(CC) $(CFLAGS) -o $@ main.c $(LDFLAGS)

clean:
	rm -f folio

.PHONY: clean
