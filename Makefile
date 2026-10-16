# Makefile for Jetson Power Modes Manager GUI
#
# A proper Linux desktop application package for managing NVIDIA Jetson
# power modes using the inbuilt nvpmodel and jetson_clocks CLI tools.
#
# Usage:
#   make install        # install system-wide (requires root / sudo)
#   make uninstall      # remove system-wide installation
#   make clean          # remove build artifacts
#
# Set DESTDIR to stage files into a custom root (e.g. make DESTDIR=/tmp/root install)

PYTHON      ?= python3
PREFIX      ?= /usr
BINDIR      ?= $(PREFIX)/bin
SHAREDIR    ?= $(PREFIX)/share/jetson-power-gui
APPSDIR     ?= $(PREFIX)/share/applications
ICONDIR     ?= $(PREFIX)/share/icons/hicolor

APP         = jetson-power-gui
MAIN_PY     = jetson_power_gui.py
DESKTOP     = $(APP).desktop
LAUNCHER    = $(APP)

# Icon sizes to install
ICON_SIZES  = 16x16 32x32 48x48 64x64 128x128 256x256 512x512

.PHONY: all install uninstall clean help

all:
	@echo "Jetson Power Modes Manager"
	@echo ""
	@echo "  make install   - install system-wide (requires root)"
	@echo "  make uninstall - remove system-wide installation"
	@echo "  make clean     - remove build artifacts"

install:
	@echo "Installing $(APP) to $(DESTDIR)$(PREFIX)..."
	# Main application directory
	install -d -m 0755 $(DESTDIR)$(SHAREDIR)
	install -m 0644 $(MAIN_PY) $(DESTDIR)$(SHAREDIR)/$(MAIN_PY)
	# Executable launcher
	install -d -m 0755 $(DESTDIR)$(BINDIR)
	install -m 0755 $(LAUNCHER) $(DESTDIR)$(BINDIR)/$(APP)
	# Desktop entry
	install -d -m 0755 $(DESTDIR)$(APPSDIR)
	install -m 0644 $(DESKTOP) $(DESTDIR)$(APPSDIR)/$(DESKTOP)
	# Icons
	@for size in $(ICON_SIZES); do \
		install -d -m 0755 $(DESTDIR)$(ICONDIR)/$$size/apps; \
		install -m 0644 icons/$$size/jetson-power-gui.png \
			$(DESTDIR)$(ICONDIR)/$$size/apps/jetson-power-gui.png; \
	done
	# Update icon cache so icons appear in application menu
	@if [ -z "$(DESTDIR)" ] && command -v gtk-update-icon-cache >/dev/null 2>&1; then \
		gtk-update-icon-cache -f -t $(DESTDIR)$(ICONDIR) 2>/dev/null || true; \
	fi
	# Update desktop database so .desktop entry is registered
	@if [ -z "$(DESTDIR)" ] && command -v update-desktop-database >/dev/null 2>&1; then \
		update-desktop-database $(DESTDIR)$(APPSDIR) 2>/dev/null || true; \
	fi
	@echo ""
	@echo "Installation complete."
	@echo "Launch from your application menu: $(APP)"
	@echo "Or run from terminal: $(APP)"

uninstall:
	@echo "Uninstalling $(APP)..."
	rm -f $(DESTDIR)$(BINDIR)/$(APP)
	rm -f $(DESTDIR)$(SHAREDIR)/$(MAIN_PY)
	rmdir $(DESTDIR)$(SHAREDIR) 2>/dev/null || true
	rm -f $(DESTDIR)$(APPSDIR)/$(DESKTOP)
	@for size in $(ICON_SIZES); do \
		rm -f $(DESTDIR)$(ICONDIR)/$$size/apps/jetson-power-gui.png; \
		rmdir $(DESTDIR)$(ICONDIR)/$$size/apps 2>/dev/null || true; \
	done
	# Refresh caches after removal
	@if [ -z "$(DESTDIR)" ] && command -v gtk-update-icon-cache >/dev/null 2>&1; then \
		gtk-update-icon-cache -f -t $(DESTDIR)$(ICONDIR) 2>/dev/null || true; \
	fi
	@if [ -z "$(DESTDIR)" ] && command -v update-desktop-database >/dev/null 2>&1; then \
		update-desktop-database $(DESTDIR)$(APPSDIR) 2>/dev/null || true; \
	fi
	@echo "Uninstallation complete."

clean:
	@echo "Nothing to build (pure Python application)."
	@echo "Run 'make uninstall' to remove installed files."

help:
	@echo "Targets: install, uninstall, clean, help"
