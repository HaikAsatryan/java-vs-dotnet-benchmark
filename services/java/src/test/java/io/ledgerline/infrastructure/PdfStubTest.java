package io.ledgerline.infrastructure;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

/**
 * spec/openapi.yaml calls the rendering FROZEN and equivalence-compares {@code bytes_written},
 * so the 32768 and the truncation point are the contract. The counterpart of
 * {@code services/dotnet/Ledgerline.Tests/PdfStubTests.cs}.
 */
class PdfStubTest {

    @ParameterizedTest
    @ValueSource(longs = {1, 42, 5_000_000, 1_999_999_999})
    void everyDocumentIsExactly32768Bytes(long invoiceId) {
        assertThat(PdfStub.render(invoiceId)).hasSize(PdfStub.TARGET_BYTES);
    }

    @Test
    void linesAreTheFrozenAsciiFormNumberedFromOne() {
        String rendered = new String(PdfStub.render(7), StandardCharsets.US_ASCII);
        assertThat(rendered).startsWith("%PDFSTUB invoice=7 line=1\n%PDFSTUB invoice=7 line=2\n");
    }

    @Test
    void theLastLineIsCutMidTextRatherThanDropped() {
        // The loop appends until the buffer REACHES 32768 and then truncates, so the final line
        // is a fragment. Stopping before the overshoot instead would give a shorter document and
        // a bytes_written that no longer matches the .NET side.
        String rendered = new String(PdfStub.render(1), StandardCharsets.US_ASCII);
        assertThat(rendered).doesNotEndWith("\n");
        assertThat(rendered.lastIndexOf('\n')).isGreaterThan(PdfStub.TARGET_BYTES - 32);
    }

    @Test
    void writeReturnsTheByteCountAndLeavesThatManyBytesOnDisk(@TempDir Path dir) throws IOException {
        int written = PdfStub.write(dir.toString(), 12);

        assertThat(written).isEqualTo(PdfStub.TARGET_BYTES);
        assertThat(dir.resolve("12.pdfstub")).hasSize(PdfStub.TARGET_BYTES);
        assertThat(Files.readAllBytes(dir.resolve("12.pdfstub"))).isEqualTo(PdfStub.render(12));
    }

    @Test
    void theReportedPathIsBuiltFromTheConfiguredDirWithForwardSlashes() {
        assertThat(PdfStub.pathFor("/data/pdf", 12)).isEqualTo("/data/pdf/12.pdfstub");
    }
}
