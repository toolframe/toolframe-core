SHELL := /bin/bash

.DEFAULT_GOAL := help

.PHONY: help build package release release-macos-arm release-macos-intel

help:
	@printf '%s\n' \
		'Available targets:' \
		'  make build                Build only the optimized host Rust binary' \
		'  make package              Build one host binary at dist/lite-mcp' \
		'  make release              Build arm64 and Intel macOS 12+ releases' \
		'  make release TARGET=...   Build one supported Rust target release' \
		'  make release-macos-arm    Build only the macOS 12+ arm64 release' \
		'  make release-macos-intel  Build only the macOS 12+ Intel release'

build:
	cargo build --release

package:
	./scripts/package.sh

release:
	./scripts/release.sh $(TARGET)

release-macos-arm:
	./scripts/release.sh aarch64-apple-darwin

release-macos-intel:
	./scripts/release.sh x86_64-apple-darwin
