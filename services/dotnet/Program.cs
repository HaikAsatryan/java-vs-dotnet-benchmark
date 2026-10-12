using Ledgerline.Cache;
using Ledgerline.Config;
using Ledgerline.Data;
using Ledgerline.Endpoints;
using Ledgerline.Infrastructure;
using Ledgerline.Logging;
using Ledgerline.Observability;
using Ledgerline.Pricing;
using Ledgerline.Problems;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging.Console;
using Npgsql;
using StackExchange.Redis;

// The template default (NOTES.md "Canonicality"). Its extra logging providers are cleared
// below (the frozen stdout contract, spec/logging.md), and it registers no static-file
// middleware, so the unmatched-path behaviour application.yaml equalizes against holds.
var builder = WebApplication.CreateBuilder(args);

var cfg = LedgerConfig.FromEnvironment();
builder.Services.AddSingleton(cfg);

builder.WebHost.ConfigureKestrel(k =>
{
   // BODY_MAX_BYTES: same limit both sides; Java enforces it in a filter because Tomcat
   // has no container-level cap for application/json (docs/FAIRNESS.md).
   k.Limits.MaxRequestBodySize = cfg.BodyMaxBytes;
   // Non-defaults (Kestrel: 30 s / 130 s), set 1:1 with application.yaml's
   // server.tomcat.connection-timeout / keep-alive-timeout: identical on both sides,
   // so neither stack times out a keep-alive connection the other keeps.
   k.Limits.RequestHeadersTimeout = TimeSpan.FromSeconds(5);
   k.Limits.KeepAliveTimeout = TimeSpan.FromSeconds(120);
   // Wire parity, not tuning: Tomcat sends no Server header, and Kestrel's default
   // `Server: Kestrel` would be a per-response header on one side only (docs/FAIRNESS.md).
   k.AddServerHeader = false;
});
builder.WebHost.UseUrls($"http://0.0.0.0:{cfg.Port}");

// Logging: custom flat-JSON console formatter (frozen schema), block-on-full bounded queue.
// ClearProviders is load-bearing under CreateBuilder: the template default adds Console,
// Debug and EventSource providers, and the stock console formatter cannot produce the
// frozen schema (spec/logging.md).
builder.Logging.ClearProviders();
builder.Logging.AddConsole(o => o.FormatterName = LedgerConsoleFormatter.FormatterName);
builder.Logging.AddConsoleFormatter<LedgerConsoleFormatter, ConsoleFormatterOptions>();
builder.Logging.Services.Configure<ConsoleLoggerOptions>(o =>
{
   o.QueueFullMode = ConsoleLoggerQueueFullMode.Wait;   // block-on-full, lossless (spec/logging.md; the default DropWrite drops)
   o.MaxQueueLength = cfg.LogQueueLen;                  // LOG_QUEUE_LEN (8192)
});
builder.Logging.SetMinimumLevel(LogLevel.Information);
// spec/logging.md: no non-contract lines during measured traffic. Without this filter the
// framework emits ~6 Information lines per request (Hosting.Diagnostics request start/finish,
// EndpointMiddleware, HttpResults) through the same bounded queue; a measured fairness bug.
// Warning floor for framework categories is also the stock ASP.NET template default.
builder.Logging.AddFilter("Microsoft.AspNetCore", LogLevel.Warning);
// EF Core logs "Executed DbCommand" (full SQL text) at Information PER QUERY,
// thousands of non-contract lines through the same bounded blocking queue during
// measured traffic: a measured fairness bug, same class as the AspNetCore filter
// above. spec/logging.md: contract lines only. The Java counterpart floors are in
// application.yaml (`logging.level.*`).
builder.Logging.AddFilter("Microsoft.EntityFrameworkCore", LogLevel.Warning);
// Hosting.Lifetime emits 5 Information lines ("Now listening on", "Application
// started", ...) at startup/shutdown: outside measured windows, but spec/logging.md
// pins the contract lines as the ONLY lines, and the Java side suppresses its own
// startup lines the same way (banner-mode off, log-startup-info false). Readiness is
// health-check-based, so nothing consumes these.
builder.Logging.AddFilter("Microsoft.Hosting.Lifetime", LogLevel.Warning);

// JSON: reflection-based System.Text.Json at ASP.NET's Web defaults, the template
// default. snake_case comes from explicit [JsonPropertyName] on every DTO member
// (spec/openapi.yaml), so no naming policy or serializer registration is needed here.

// ONE shared NpgsqlDataSource for BOTH data layers (EF Core + raw ADO.NET).
// A bare UseNpgsql(connString) would open a SECOND, connstring-keyed pool next
// to the data-source pool: two pools per process (the Java side runs exactly
// one HikariCP pool) and split db.client.connection.* metrics. Single
// registration, single pool.
var connString = NpgsqlConnectionString.Build(cfg);
var dataSource = new NpgsqlDataSourceBuilder(connString).Build();
builder.Services.AddSingleton(dataSource);

// Pooled DbContext: non-default (AddDbContext is the baseline registration) with no
// Hibernate analogue, disclosed in docs/FAIRNESS.md. The model is built at startup
// from OnModelCreating, stock EF.
builder.Services.AddDbContextPool<LedgerDbContext>(o => o.UseNpgsql(dataSource));

builder.Services.AddSingleton<IConnectionMultiplexer>(_ =>
{
   var options = ConfigurationOptions.Parse(cfg.RedisAddr);
   // Non-default (SE.Redis aborts the initial connect by default): behaviour parity with
   // Lettuce, which connects lazily and reconnects in the background rather than failing
   // startup. Readiness is /readyz's Redis PING on both sides, not client construction.
   options.AbortOnConnectFail = false;
   // spec/cache.md freezes the invalidation as DEL. SE.Redis prefers UNLINK wherever the server
   // supports it, and UNLINK frees the value on a background thread where DEL frees it inline:
   // a different Redis-side cost on the create path, not a spelling. Taking UNLINK off the map
   // is the documented route back to DEL. (The read path's SETEX is the same operation as the
   // spec's SET..EX and stays: see docs/FAIRNESS.md.)
   options.CommandMap = CommandMap.Create(new HashSet<string> { "UNLINK" }, available: false);
   return ConnectionMultiplexer.Connect(options);
});
builder.Services.AddSingleton(sp => new InvoiceCache(sp.GetRequiredService<IConnectionMultiplexer>(), cfg.CacheTtl));

// DATA_LAYER=efcore|ado|dapper DI switch (efcore is the headline; ado and dapper remain
// as code and no measured cell uses them, PROGRESS.md D5).
builder.Services.AddScoped<IInvoiceRepository>(sp => cfg.DataLayer switch
{
   DataLayer.Ado => new AdoInvoiceRepository(sp.GetRequiredService<NpgsqlDataSource>()),
   DataLayer.Dapper => new DapperInvoiceRepository(sp.GetRequiredService<NpgsqlDataSource>()),
   _ => new EfInvoiceRepository(sp.GetRequiredService<LedgerDbContext>()),
});

builder.Services.AddSingleton<PricingEngine>();
builder.Services.AddSingleton<RuntimeStatsCollector>();

builder.Services.AddValidation();
builder.Services.AddProblemDetails(ProblemDetailsConfig.Configure);
builder.Services.AddExceptionHandler<ProblemExceptionHandler>();

// Malformed-body handling must throw (BadHttpRequestException/JsonException ->
// ProblemExceptionHandler -> the frozen 400 {"body":["is required"]} + request_error log).
// ThrowOnBadRequest defaults to true only in Development; without this pin the Production
// container would emit a bare framework 400 with none of the frozen contract.
builder.Services.Configure<RouteHandlerOptions>(o => o.ThrowOnBadRequest = true);

var app = builder.Build();

app.Services.GetRequiredService<RuntimeStatsCollector>().Start();

// PDF_DIR created once at startup on BOTH services; a directory that disappears
// mid-run is a 500 on both sides, not a silent re-create.
Directory.CreateDirectory(cfg.PdfDir);

// Warm the shared pool to min=max BEFORE serving traffic (spec/db-access.md:
// both services open all DB_POOL_MAX connections concurrently at startup; the
// warmup gate asserts db_pool.total == max). Npgsql has no background min-filler:
// MinPoolSize only floors pruning, it never pre-opens. Open all DbPoolMax
// connections concurrently once, then return them to the pool as idle.
if (cfg.DbPoolWarmOnStart)
{
   var warm = new NpgsqlConnection[cfg.DbPoolMax];
   await Task.WhenAll(Enumerable.Range(0, cfg.DbPoolMax).Select(async i =>
   {
      warm[i] = await dataSource.OpenConnectionAsync();
   }));
   foreach (var c in warm)
      await c.DisposeAsync();
}

app.UseMiddleware<RequestIdMiddleware>();
app.UseMiddleware<AccessLogMiddleware>();
// The content-type gate sits inside the access-log wrapper (its 415s still get an
// access line) and before the exception/status-code writers.
app.UseMiddleware<MediaTypeGateMiddleware>();
app.UseExceptionHandler();
app.UseStatusCodePages();

app.MapInvoiceEndpoints();
app.MapCustomerEndpoints();
app.MapPricingEndpoints();
app.MapHealthEndpoints();
app.MapRuntimeStatsEndpoints();

// Effective-config line (manifest-captured), once at startup.
EffectiveConfig.LogOnce(app.Services.GetRequiredService<ILoggerFactory>().CreateLogger("Ledgerline"), cfg);

// Graceful shutdown: ApplicationStopped fires AFTER in-flight requests drain (parity
// with the Java side's server.shutdown=graceful; spec/logging.md pins shutdown_complete
// as the last line after the drain); the console queue is flushed when the provider
// disposes, after this callback.
var lifetime = app.Services.GetRequiredService<IHostApplicationLifetime>();
lifetime.ApplicationStopped.Register(() =>
{
   app.Services.GetRequiredService<RuntimeStatsCollector>().Dispose();
   LedgerLog.ShutdownComplete(app.Services.GetRequiredService<ILoggerFactory>().CreateLogger("Ledgerline"));
});

app.Run();

// Exposed for WebApplicationFactory-based tests.
public partial class Program;
