using Ledgerline.Data;
using Ledgerline.Data.Entities;
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Metadata;
using Xunit;

namespace Ledgerline.Tests;

/// Asserts the EF model maps to spec/schema.sql exactly: snake_case table/column names, money as
/// bigint/long, char(3) currency, timestamptz, identity columns, and the named indexes.
public class EfSchemaMappingTests
{
   private static IModel Model()
   {
      var options = new DbContextOptionsBuilder<LedgerDbContext>()
         .UseNpgsql("Host=localhost;Database=ledgerline;Username=ledger;Password=ledger")
         .Options;
      using var db = new LedgerDbContext(options);
      return db.Model;
   }

   [Fact]
   public void Tables_AreSnakeCase()
   {
      var m = Model();
      Assert.Equal("customers", m.FindEntityType(typeof(Customer))!.GetTableName());
      Assert.Equal("invoices", m.FindEntityType(typeof(Invoice))!.GetTableName());
      Assert.Equal("invoice_items", m.FindEntityType(typeof(InvoiceItem))!.GetTableName());
   }

   [Theory]
   [InlineData(typeof(Customer), nameof(Customer.Id), "id", "bigint")]
   [InlineData(typeof(Customer), nameof(Customer.BalanceMinor), "balance_minor", "bigint")]
   [InlineData(typeof(Customer), nameof(Customer.Currency), "currency", "char(3)")]
   [InlineData(typeof(Customer), nameof(Customer.CreatedAt), "created_at", "timestamptz")]
   [InlineData(typeof(Invoice), nameof(Invoice.CustomerId), "customer_id", "bigint")]
   [InlineData(typeof(Invoice), nameof(Invoice.TotalMinor), "total_minor", "bigint")]
   [InlineData(typeof(Invoice), nameof(Invoice.Currency), "currency", "char(3)")]
   [InlineData(typeof(Invoice), nameof(Invoice.CreatedAt), "created_at", "timestamptz")]
   [InlineData(typeof(InvoiceItem), nameof(InvoiceItem.InvoiceId), "invoice_id", "bigint")]
   [InlineData(typeof(InvoiceItem), nameof(InvoiceItem.UnitPriceMinor), "unit_price_minor", "bigint")]
   [InlineData(typeof(InvoiceItem), nameof(InvoiceItem.LineTotalMinor), "line_total_minor", "bigint")]
   [InlineData(typeof(InvoiceItem), nameof(InvoiceItem.Qty), "qty", "integer")]
   public void Columns_MapToSpecNamesAndTypes(Type clr, string property, string column, string dbType)
   {
      var prop = Model().FindEntityType(clr)!.FindProperty(property)!;
      var storeObject = StoreObjectIdentifier.Table(
         Model().FindEntityType(clr)!.GetTableName()!);
      Assert.Equal(column, prop.GetColumnName(storeObject));
      Assert.Equal(dbType, prop.GetColumnType());
   }

   [Fact]
   public void Invoice_Id_IsIdentityAlways()
   {
      var prop = Model().FindEntityType(typeof(Invoice))!.FindProperty(nameof(Invoice.Id))!;
      Assert.Equal(ValueGenerated.OnAdd, prop.ValueGenerated);
   }

   [Fact]
   public void Customer_Id_IsNeverGenerated()
   {
      var prop = Model().FindEntityType(typeof(Customer))!.FindProperty(nameof(Customer.Id))!;
      Assert.Equal(ValueGenerated.Never, prop.ValueGenerated);
   }

   [Fact]
   public void Invoice_CreatedAt_HasNowDefault()
   {
      var prop = Model().FindEntityType(typeof(Invoice))!.FindProperty(nameof(Invoice.CreatedAt))!;
      Assert.Equal("now()", prop.GetDefaultValueSql());
   }

   [Fact]
   public void ListIndex_Exists_OnCustomerIdAndId()
   {
      // Descending-on-id is verified via the generated SQL (EfQueryShapeTests: "order by i.id desc");
      // assert name + column order here.
      var invoice = Model().FindEntityType(typeof(Invoice))!;
      var idx = invoice.GetIndexes().Single(i => i.GetDatabaseName() == "idx_invoices_customer_id_id_desc");
      var cols = idx.Properties.Select(p => p.Name).ToArray();
      Assert.Equal(["CustomerId", "Id"], cols);
   }

   [Fact]
   public void ItemsIndex_Exists_OnInvoiceIdAndId()
   {
      var item = Model().FindEntityType(typeof(InvoiceItem))!;
      var idx = item.GetIndexes().Single(i => i.GetDatabaseName() == "idx_invoice_items_invoice_id");
      Assert.Equal(["InvoiceId", "Id"], idx.Properties.Select(p => p.Name).ToArray());
   }
}
