using System.Text;
using Ledgerline.Infrastructure;
using Xunit;

namespace Ledgerline.Tests;

public class PdfStubTests
{
   [Theory]
   [InlineData(1)]
   [InlineData(42)]
   [InlineData(5_000_000)]
   [InlineData(9_223_372_036_854_775_807)]
   public void Render_IsExactly32768Bytes(long invoiceId)
   {
      var bytes = PdfStub.Render(invoiceId);
      Assert.Equal(32768, bytes.Length);
   }

   [Fact]
   public void Render_BeginsWithFrozenLineFormat()
   {
      var bytes = PdfStub.Render(42);
      var text = Encoding.ASCII.GetString(bytes);
      Assert.StartsWith("%PDFSTUB invoice=42 line=1\n%PDFSTUB invoice=42 line=2\n", text);
   }

   [Fact]
   public void Render_IsDeterministic()
   {
      Assert.Equal(PdfStub.Render(7), PdfStub.Render(7));
   }

   [Fact]
   public void Render_TruncatesMidLineWhenNeeded()
   {
      // The total must be exactly 32768 even though whole lines rarely sum to it.
      var bytes = PdfStub.Render(123456);
      Assert.Equal(32768, bytes.Length);
      // Every byte is printable ASCII or newline (no embedded nulls from truncation).
      Assert.All(bytes, b => Assert.True(b == (byte)'\n' || (b >= 32 && b < 127)));
   }

   [Fact]
   public void PathFor_UsesCanonicalDataPdfPath()
   {
      Assert.Equal("/data/pdf/42.pdfstub", PdfStub.PathFor("/data/pdf", 42));
   }
}
