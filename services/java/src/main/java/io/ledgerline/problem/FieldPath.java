package io.ledgerline.problem;

/**
 * Jakarta property path to the frozen errors-map KEY. spec/openapi.yaml (Problem.errors, "KEY
 * FORMAT") freezes them as .NET emits them ({@code Items[0].Sku}) and Jakarta builds paths from
 * the JAVA component names, so the transform is capitalising each dot-separated segment.
 *
 * <p>Applied in {@link LedgerExceptionHandler}, not by overriding
 * {@code SpringValidatorAdapter.determineField}: the adapter matches violations to binding
 * failures by the UNRENAMED path, so renaming inside it double-reports a field that fails both.
 */
public final class FieldPath {

    private FieldPath() {
    }

    public static String toClrPath(String jakartaPath) {
        StringBuilder out = new StringBuilder(jakartaPath.length());
        boolean atSegmentStart = true;
        for (int i = 0; i < jakartaPath.length(); i++) {
            char c = jakartaPath.charAt(i);
            out.append(atSegmentStart ? Character.toUpperCase(c) : c);
            atSegmentStart = c == '.';
        }
        return out.toString();
    }
}
