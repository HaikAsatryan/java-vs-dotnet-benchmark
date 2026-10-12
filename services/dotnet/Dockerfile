# services/dotnet/Dockerfile: Ledgerline .NET SUT.
# SDK build -> standard aspnet runtime. Image digests pinned 2026-08-13 (SDK) / 2026-08-27
# (runtime).
#
# The runtime is the standard mcr.microsoft.com/dotnet/aspnet image, not the chiseled one,
# and InvariantGlobalization is NOT set: the Java side ships a full JRE with ICU, and a
# full aspnet image with ICU is what an ordinary team deploys. Trimming the .NET runtime
# down (chiseled userland, invariant globalization) while Java keeps its full posture
# would be a one-sided footprint optimisation. Disclosed in docs/FAIRNESS.md.

FROM mcr.microsoft.com/dotnet/sdk:10.0@sha256:e1fc6e423f543119c406d24e2e687d67c569f18f04a37a8b0005d80ad0dcee80 AS build
WORKDIR /src

# Restore against the csproj only first (layer cache). The test project lives in a subfolder and
# is excluded from the web app compile (csproj Compile Remove), so it never enters the image.
COPY Ledgerline.csproj ./
RUN dotnet restore Ledgerline.csproj

COPY . .
RUN dotnet publish Ledgerline.csproj -c Release -o /app --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0-noble@sha256:a4556ed033fa96f984bb7a8d348851cb2d36b1281dd2420070045f664fbb5f94
WORKDIR /app
# Pre-owned pdf-stub dir, chowned to the image's non-root `app` user (uid 1654, the same
# uid services/java/Dockerfile creates) so the two pdf-stub volumes have identical
# ownership semantics.
RUN mkdir -p /data/pdf && chown -R 1654:1654 /data
COPY --from=build /app .

# Non-root (.NET images set APP_UID=1654). PDF volume mount target is /data/pdf (PDF_DIR default).
# The base image sets ASPNETCORE_HTTP_PORTS=8080; cleared so Program.cs's UseUrls binds
# 0.0.0.0:{PORT} authoritatively WITHOUT the framework's "Overriding HTTP_PORTS" warn line —
# a non-contract line on the frozen stdout stream (spec/logging.md), verified both ways.
ENV ASPNETCORE_HTTP_PORTS=
USER $APP_UID
EXPOSE 8080
ENTRYPOINT ["./Ledgerline"]
