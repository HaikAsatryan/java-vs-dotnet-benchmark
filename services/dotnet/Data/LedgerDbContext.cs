using Ledgerline.Data.Entities;
using Microsoft.EntityFrameworkCore;

namespace Ledgerline.Data;

public sealed class LedgerDbContext : DbContext
{
   public LedgerDbContext(DbContextOptions<LedgerDbContext> options) : base(options)
   {
      // Non-default, kept for statement-stream parity, not speed: at EF's default a
      // SaveChanges inside the explicit create transaction wraps itself in a SAVEPOINT,
      // an extra wire command inside the tx that Hibernate never issues, and
      // spec/db-access.md freezes the create at exactly Q7/BEGIN/Q4/items/Q6/COMMIT
      // ("Automatic savepoints are disabled"). Disclosed in docs/FAIRNESS.md.
      Database.AutoSavepointsEnabled = false;
   }

   public DbSet<Customer> Customers => Set<Customer>();
   public DbSet<Invoice> Invoices => Set<Invoice>();
   public DbSet<InvoiceItem> InvoiceItems => Set<InvoiceItem>();

   protected override void OnModelCreating(ModelBuilder b)
   {
      b.Entity<Customer>(e =>
      {
         e.ToTable("customers");
         e.HasKey(x => x.Id);
         e.Property(x => x.Id).HasColumnName("id").HasColumnType("bigint").ValueGeneratedNever();
         e.Property(x => x.Name).HasColumnName("name").HasColumnType("text").IsRequired();
         e.Property(x => x.Email).HasColumnName("email").HasColumnType("text").IsRequired();
         e.Property(x => x.BalanceMinor).HasColumnName("balance_minor").HasColumnType("bigint")
            .IsRequired().HasDefaultValue(0L);
         e.Property(x => x.Currency).HasColumnName("currency").HasColumnType("char(3)").IsRequired();
         e.Property(x => x.CreatedAt).HasColumnName("created_at").HasColumnType("timestamptz").IsRequired();
      });

      b.Entity<Invoice>(e =>
      {
         e.ToTable("invoices");
         e.HasKey(x => x.Id);
         e.Property(x => x.Id).HasColumnName("id").HasColumnType("bigint").ValueGeneratedOnAdd()
            .UseIdentityAlwaysColumn();
         e.Property(x => x.CustomerId).HasColumnName("customer_id").HasColumnType("bigint").IsRequired();
         e.Property(x => x.Status).HasColumnName("status").HasColumnType("text").IsRequired();
         e.Property(x => x.Currency).HasColumnName("currency").HasColumnType("char(3)").IsRequired();
         e.Property(x => x.TotalMinor).HasColumnName("total_minor").HasColumnType("bigint").IsRequired();
         e.Property(x => x.CreatedAt).HasColumnName("created_at").HasColumnType("timestamptz")
            .IsRequired().ValueGeneratedOnAdd().HasDefaultValueSql("now()");

         e.HasOne<Customer>().WithMany().HasForeignKey(x => x.CustomerId);
         e.HasMany(x => x.Items).WithOne().HasForeignKey(x => x.InvoiceId);

         e.HasIndex(x => new { x.CustomerId, x.Id }).HasDatabaseName("idx_invoices_customer_id_id_desc")
            .IsDescending(false, true);
      });

      b.Entity<InvoiceItem>(e =>
      {
         e.ToTable("invoice_items");
         e.HasKey(x => x.Id);
         e.Property(x => x.Id).HasColumnName("id").HasColumnType("bigint").ValueGeneratedOnAdd()
            .UseIdentityAlwaysColumn();
         e.Property(x => x.InvoiceId).HasColumnName("invoice_id").HasColumnType("bigint").IsRequired();
         e.Property(x => x.Sku).HasColumnName("sku").HasColumnType("text").IsRequired();
         e.Property(x => x.Description).HasColumnName("description").HasColumnType("text").IsRequired();
         e.Property(x => x.Qty).HasColumnName("qty").HasColumnType("integer").IsRequired();
         e.Property(x => x.UnitPriceMinor).HasColumnName("unit_price_minor").HasColumnType("bigint").IsRequired();
         e.Property(x => x.LineTotalMinor).HasColumnName("line_total_minor").HasColumnType("bigint").IsRequired();

         e.HasIndex(x => new { x.InvoiceId, x.Id }).HasDatabaseName("idx_invoice_items_invoice_id");
      });
   }
}
