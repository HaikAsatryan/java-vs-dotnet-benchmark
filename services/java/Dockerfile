# services/java/Dockerfile: Ledgerline Java SUT.
# Temurin JDK build -> Temurin JRE runtime. Image digests pinned 2026-08-23.
#
# The JRE tag is 25.0.4_7, which is the exact build the measurement host runs (Temurin
# 25.0.4+7), so a measured number and a locally reproduced one come off the same JVM.
# `-noble` stays EXPLICIT: the floating 25 / 25-jre tags resolve to -resolute (Ubuntu 26.04),
# which would change the userland under the SUT without changing this file.

FROM eclipse-temurin:25.0.4_7-jdk-noble@sha256:534968c051301957beae735e7ba1db54d99ddecf08746d3b9d4f318cc132dbc3 AS build
WORKDIR /src

# Resolve against the POM alone first, so a source-only change does not re-download the tree.
COPY .mvn/ .mvn/
COPY mvnw pom.xml ./
RUN ./mvnw -B -ntp dependency:go-offline

# Test sources are excluded by .dockerignore and the build skips compiling them, mirroring
# services/dotnet/Ledgerline.csproj's <Compile Remove> of the test project: the test tree never
# enters the image and its dependencies never enter the resolved classpath.
COPY src/ src/
RUN ./mvnw -B -ntp -Dmaven.test.skip=true package

# No AOT cache, no CDS archive, no Spring AOT processing. This is a DEVIATION from Spring Boot
# 4.1's own container-images guidance, which now recommends a training run, and it is
# deliberate: the .NET side ships without ReadyToRun and without Native AOT, so both stacks run
# JIT-from-cold and the cold-start curve compares like with like. Adding it here would be a
# one-sided startup optimisation. Disclosed in docs/FAIRNESS.md.

FROM eclipse-temurin:25.0.4_7-jre-noble@sha256:b4c93a50fc67612798db73d68ca3b0ee4ebdd51736e59cca370e689b9797037e
WORKDIR /app

# Same uid as the .NET image so the two pdf-stub volumes have identical ownership semantics.
RUN groupadd --gid 1654 ledgerline \
 && useradd --uid 1654 --gid 1654 --no-create-home --shell /usr/sbin/nologin ledgerline \
 && mkdir -p /data/pdf \
 && chown -R 1654:1654 /data

COPY --from=build /src/target/ledgerline.jar /app/ledgerline.jar

USER 1654
EXPOSE 8080
# Exec form: JAVA_OPTS and JAVA_TOOL_OPTIONS are INERT here (no shell expands the first, and
# the second is asserted empty by BuildInvariantsTest). The harness passes every JVM flag
# through JDK_JAVA_OPTIONS, which the `java` launcher PREPENDS to this command line, so a
# compose overlay can override a base-file value and the manifest records exactly one carrier
# (bench/ledgerbench/registry.py).
ENTRYPOINT ["java", "-jar", "/app/ledgerline.jar"]
