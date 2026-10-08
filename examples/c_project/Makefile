CC ?= cc
CFLAGS ?= -Wall -Wextra -std=c11 -g

all: example

example: main.c
	$(CC) $(CFLAGS) -o $@ $<

test: example
	./example

clean:
	rm -f example example.exe

.PHONY: all test clean
