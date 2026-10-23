# Maven `pom.xml` Compliance Guidelines

Applies only if a `pom.xml` file was found in the repo (i.e. this is a
Maven project). If no `pom.xml` was found, skip every check in this file
entirely — do not report FAIL rows for a build system the project
doesn't use.

These checks require the full content of `pom.xml` (via `read_file`) —
an outline is not sufficient, since the checks depend on actual
dependency/version/URL values.

## Checks

1. **No `SNAPSHOT` dependency versions** (severity: HIGH) — No
   `<dependency>` or the project's own `<version>` should end in
   `-SNAPSHOT`. A `-SNAPSHOT` version is a FAIL (fine for local
   development, not for a tracked release build).

2. **Parent POM not ancient** (severity: LOW) — If a `<parent>` element
   is present (e.g. `spring-boot-starter-parent`,
   `quarkus-bom`), its `<version>` should look like a reasonably recent
   release, not a version with a major version number at least 2 behind
   the current major line for that artifact, if you can tell from the
   version string alone. If you cannot confidently judge "too old"
   without external knowledge, mark this check SKIP rather than
   guessing.

3. **No insecure (plain HTTP) repository URLs** (severity: HIGH) — Any
   `<repository>` or `<pluginRepository>` `<url>` must use `https://`,
   not `http://`. A plain-HTTP URL is a FAIL.

4. **Dependency versions pinned, not ranges** (severity: MEDIUM) — No
   `<dependency>` `<version>` should use a Maven version range (e.g.
   `[1.0,2.0)`, `1.0.+`) — ranges make builds non-reproducible. A range
   expression is a FAIL.

5. **Plugin versions explicitly declared** (severity: LOW) — Every
   `<plugin>` under `<build><plugins>` should declare an explicit
   `<version>`. A plugin entry with no `<version>` (relying on Maven's
   default resolution) is a FAIL.
