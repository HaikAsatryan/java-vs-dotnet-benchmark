using System.Text.RegularExpressions;
using Ledgerline.Sql;
using Xunit;

namespace Ledgerline.Tests;

/// Asserts that the Dapper variant's named-parameter SQL reduces to the EXACT canonical
/// statement text the other layers send. Npgsql rewrites each @name to a positional $n in
/// order of first appearance within a statement; this test performs the same substitution
/// and compares byte-for-byte against SqlStatements (which is itself pinned to
/// spec/db-access.sql by SqlParityTests).
public partial class DapperSqlParityTests
{
   [GeneratedRegex(@"@\w+")]
   private static partial Regex NamedParam();

   /// Replace each named parameter with $n numbered by FIRST APPEARANCE (repeats reuse
   /// their number): Npgsql's rewriting rule for one statement.
   private static string Positionalize(string sql)
   {
      var order = new Dictionary<string, int>();
      return NamedParam().Replace(sql, m =>
      {
         if (!order.TryGetValue(m.Value, out var n))
         {
            n = order.Count + 1;
            order[m.Value] = n;
         }
         return $"${n}";
      });
   }

   [Fact]
   public void GetInvoiceWithItems_IsQ1ThenQ2()
   {
      // The multi-statement command splits at ";\n"; each statement is rewritten
      // independently (per-statement parameter numbering).
      var parts = DapperSql.GetInvoiceWithItems.Split(";\n");
      Assert.Equal(2, parts.Length);
      Assert.Equal(SqlStatements.GetInvoice, Positionalize(parts[0]));
      Assert.Equal(SqlStatements.GetInvoiceItems, Positionalize(parts[1]));
   }

   [Fact]
   public void GetInvoice_IsQ1() =>
      Assert.Equal(SqlStatements.GetInvoice, Positionalize(DapperSql.GetInvoice));

   [Fact]
   public void ListInvoicesByCustomer_IsQ3() =>
      Assert.Equal(SqlStatements.ListInvoicesByCustomer, Positionalize(DapperSql.ListInvoicesByCustomer));

   [Fact]
   public void CustomerExists_IsQ7() =>
      Assert.Equal(SqlStatements.CustomerExists, Positionalize(DapperSql.CustomerExists));

   [Fact]
   public void InsertInvoice_IsQ4() =>
      Assert.Equal(SqlStatements.InsertInvoice, Positionalize(DapperSql.InsertInvoice));
}
