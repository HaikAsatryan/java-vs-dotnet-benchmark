using System.Runtime.CompilerServices;

namespace Ledgerline.Tests;

/// Test-host environment: WebApplicationFactory boots the REAL Program, which
/// warms the DB pool at startup (DB_POOL_WARM_ON_START, default on) and creates
/// PDF_DIR. The unit tests run with no PostgreSQL and no /data volume, so the
/// warm is disabled and PDF_DIR is pointed at a writable temp dir process-wide
/// before any factory boots.
internal static class TestEnv
{
   [ModuleInitializer]
   internal static void Init()
   {
      Environment.SetEnvironmentVariable("DB_POOL_WARM_ON_START", "0");
      Environment.SetEnvironmentVariable(
         "PDF_DIR", Path.Combine(Path.GetTempPath(), "ledgerline-tests-pdf"));
   }
}
