SHELL := /bin/bash

.DEFAULT_GOAL := help

.PHONY: help build release

help:
	@printf '%s\n' \
		'Available targets:' \
		'  make build    Build only the optimized Rust binary' \
		'  make release  Build the complete distributable folder'

build:
	cargo build --release

release:
	./scripts/package.sh
