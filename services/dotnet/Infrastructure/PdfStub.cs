using System.Text;

namespace Ledgerline.Infrastructure;

/// FROZEN pdf-stub rendering (spec/openapi.yaml). Build ASCII lines
/// "%PDFSTUB invoice=<id> line=<k>\n" for k=1,2,... appended until the buffer reaches or exceeds
/// 32768 bytes, then truncate to EXACTLY 32768. One buffered write; no fsync. bytes_written = 32768.
public static class PdfStub
{
   public const int TargetBytes = 32768;

   public static byte[] Render(long invoiceId)
   {
      var sb = new StringBuilder(TargetBytes + 64);
      var k = 1;
      while (sb.Length < TargetBytes)
      {
         sb.Append("%PDFSTUB invoice=").Append(invoiceId).Append(" line=").Append(k).Append('\n');
         k++;
      }

      // ASCII-only content, so 1 char == 1 byte; truncate the char buffer to exactly TargetBytes.
      var bytes = Encoding.ASCII.GetBytes(sb.ToString());
      if (bytes.Length > TargetBytes)
         Array.Resize(ref bytes, TargetBytes);
      return bytes;
   }

   public static async Task<int> WriteAsync(string pdfDir, long invoiceId, CancellationToken ct)
   {
      // PDF_DIR is created once at startup (Program.cs), on both services.
      var bytes = Render(invoiceId);
      var path = Path.Combine(pdfDir, $"{invoiceId}.pdfstub");
      // Buffered write, no fsync / no flush-to-disk call.
      await File.WriteAllBytesAsync(path, bytes, ct);
      return bytes.Length;
   }

   // Canonical forward-slash form, built from the configured dir: identical `path` text
   // on both services for the same PDF_DIR.
   public static string PathFor(string pdfDir, long invoiceId) => $"{pdfDir}/{invoiceId}.pdfstub";
}
