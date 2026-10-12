using Ledgerline.Sql;
using Xunit;

namespace Ledgerline.Tests;

/// Asserts the .NET raw ADO.NET SQL constants are byte-identical to the bodies in spec/db-access.sql
/// (the single canonical SQL source). Parses the spec file by its `-- name:` annotations and
/// compares each statement body (everything between the annotation line and the next annotation
/// or EOF, trimmed of surrounding blank lines), so query SHAPE cannot drift across languages.
public class SqlParityTests
{
   private static readonly Dictionary<string, string> SpecBodies = ParseSpec();

   [Theory]
   [InlineData("GetInvoice")]
   [InlineData("GetInvoiceItems")]
   [InlineData("ListInvoicesByCustomer")]
   [InlineData("CustomerExists")]
   [InlineData("InsertInvoice")]
   [InlineData("InsertInvoiceItem")]
   [InlineData("UpdateCustomerBalance")]
   public void Constant_MatchesSpecBodyByteForByte(string name)
   {
      var constant = name switch
      {
         "GetInvoice" => SqlStatements.GetInvoice,
         "GetInvoiceItems" => SqlStatements.GetInvoiceItems,
         "ListInvoicesByCustomer" => SqlStatements.ListInvoicesByCustomer,
         "CustomerExists" => SqlStatements.CustomerExists,
         "InsertInvoice" => SqlStatements.InsertInvoice,
         "InsertInvoiceItem" => SqlStatements.InsertInvoiceItem,
         "UpdateCustomerBalance" => SqlStatements.UpdateCustomerBalance,
         _ => throw new ArgumentOutOfRangeException(nameof(name)),
      };

      Assert.True(SpecBodies.TryGetValue(name, out var specBody), $"spec body for {name} not found");
      Assert.Equal(specBody, constant);
   }

   [Fact]
   public void AllSpecQueriesAreCovered()
   {
      // Every named query in the spec has a matching constant (no spec query silently dropped).
      var covered = new[]
      {
         "GetInvoice", "GetInvoiceItems", "ListInvoicesByCustomer", "CustomerExists",
         "InsertInvoice", "InsertInvoiceItem", "UpdateCustomerBalance",
      };
      Assert.Equal(covered.OrderBy(x => x), SpecBodies.Keys.OrderBy(x => x));
   }

   private static Dictionary<string, string> ParseSpec()
   {
      var path = SpecPaths.DbAccessSql;
      // Read raw bytes, normalize to LF (the spec uses LF), so the comparison is newline-exact.
      var text = File.ReadAllText(path).Replace("\r\n", "\n");
      var lines = text.Split('\n');

      var bodies = new Dictionary<string, string>();
      string? current = null;
      var sb = new List<string>();

      void Flush()
      {
         if (current is null)
            return;
         // Trim leading/trailing blank lines, join with LF (matches the constants' \n joins).
         var body = sb.SkipWhile(string.IsNullOrEmpty).ToList();
         while (body.Count > 0 && string.IsNullOrEmpty(body[^1]))
            body.RemoveAt(body.Count - 1);
         // Strip the spec file's trailing ';' separator; the SAME normalization the Java
         // side's SqlParityTest applies, so both languages pin the identical wire text
         // (statements are sent without the separator on both sides).
         bodies[current] = string.Join("\n", body).TrimEnd(';');
         sb.Clear();
      }

      foreach (var line in lines)
      {
         if (line.StartsWith("-- name:", StringComparison.Ordinal))
         {
            Flush();
            // "-- name: GetInvoice :one" -> "GetInvoice"
            var rest = line["-- name:".Length..].Trim();
            current = rest.Split(' ', StringSplitOptions.RemoveEmptyEntries)[0];
            continue;
         }
         if (current is not null && line.StartsWith("--", StringComparison.Ordinal))
            continue; // skip stray comments inside a body region (none expected)
         if (current is not null)
            sb.Add(line);
      }
      Flush();

      return bodies;
   }
}
