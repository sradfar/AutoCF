# AutoCF 1.0.0 macOS Intel

Status: **native build configured; GitHub macOS build not yet executed**.

This folder is not a user-downloadable `.app` until the workflow produces
`AutoCF-1.0.0-macOS-x64.dmg` on the `macos-15-intel` runner. The build embeds
native PySide6/QtWebEngine, checks the helper/frameworks, signs the `.app`, and
creates and hashes the DMG. Public distribution additionally requires Apple
Developer signing/notarization secrets; tester builds use ad-hoc signing.

