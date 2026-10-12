package io.ledgerline.infrastructure;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * The FROZEN pdf-stub rendering of spec/openapi.yaml: {@code "%PDFSTUB invoice=<id> line=<k>\n"}
 * for k = 1, 2, ... until the buffer reaches 32768 bytes, then truncated to exactly 32768, written
 * with one buffered write and no fsync. {@code bytes_written} is equivalence-compared, so the
 * truncation point is contract rather than detail.
 */
public final class PdfStub {

    public static final int TARGET_BYTES = 32768;

    private PdfStub() {
    }

    public static byte[] render(long invoiceId) {
        StringBuilder builder = new StringBuilder(TARGET_BYTES + 64);
        int line = 1;
        while (builder.length() < TARGET_BYTES) {
            builder.append("%PDFSTUB invoice=").append(invoiceId).append(" line=").append(line)
                    .append('\n');
            line++;
        }
        // The content is ASCII, so one char is one byte and the char buffer can be cut at
        // TARGET_BYTES directly - the same equivalence US-ASCII encoding relies on in PdfStub.cs.
        return builder.substring(0, TARGET_BYTES).getBytes(StandardCharsets.US_ASCII);
    }

    /**
     * {@code Files.write} without SYNC/DSYNC: buffered, no flush-to-disk call, matching
     * {@code File.WriteAllBytesAsync}. PDF_DIR itself is created once at startup.
     */
    public static int write(String pdfDir, long invoiceId) throws IOException {
        byte[] bytes = render(invoiceId);
        Files.write(Path.of(pdfDir, invoiceId + ".pdfstub"), bytes);
        return bytes.length;
    }

    /** Forward-slash form built from the configured dir, byte-for-byte {@code PdfStub.PathFor}. */
    public static String pathFor(String pdfDir, long invoiceId) {
        return pdfDir + "/" + invoiceId + ".pdfstub";
    }
}
