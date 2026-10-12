namespace Ledgerline.Tests;

/// Locates the repo's spec/ directory from the test bin folder by walking up to the dir that
/// contains a `spec` subfolder. Keeps the SQL-parity / schema tests anchored to the single
/// canonical source rather than copies.
public static class SpecPaths
{
   public static string SpecDir { get; } = FindSpecDir();

   public static string DbAccessSql => Path.Combine(SpecDir, "db-access.sql");
   public static string SchemaSql => Path.Combine(SpecDir, "schema.sql");

   private static string FindSpecDir()
   {
      var dir = new DirectoryInfo(AppContext.BaseDirectory);
      while (dir is not null)
      {
         var candidate = Path.Combine(dir.FullName, "spec");
         if (Directory.Exists(candidate) && File.Exists(Path.Combine(candidate, "db-access.sql")))
            return candidate;
         dir = dir.Parent;
      }
      throw new DirectoryNotFoundException("Could not locate spec/ directory above the test bin folder.");
   }
}
