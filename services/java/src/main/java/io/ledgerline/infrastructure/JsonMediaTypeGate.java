package io.ledgerline.infrastructure;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.util.List;
import java.util.Locale;
import org.springframework.core.MethodParameter;
import org.springframework.web.HttpMediaTypeNotSupportedException;
import org.springframework.web.bind.annotation.RequestBody;
import org.jspecify.annotations.Nullable;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.servlet.HandlerInterceptor;

/**
 * The FROZEN content-type gate of spec/openapi.yaml x-error-strings.unsupported_media_type,
 * written as the same trivial grammar as {@code MediaTypeGateMiddleware.IsJsonMediaType} so no
 * parser-library disagreement can split the two services. Spring's own negotiation accepts a
 * DIFFERENT set - it parses the header into a MediaType, so {@code "application/json; charset"}
 * raises InvalidMediaTypeException where the frozen grammar accepts it - so the gate runs first
 * and Spring's 415 stays the fallback.
 *
 * <p>A HandlerInterceptor, not a filter: {@code preHandle} runs after handler selection, so the
 * gate asks the HANDLER whether it takes a body instead of carrying a path list (the question
 * .NET answers with {@code IAcceptsMetadata}), and rejection travels the ordinary exception path.
 */
public class JsonMediaTypeGate implements HandlerInterceptor {

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response,
            Object handler) throws HttpMediaTypeNotSupportedException {
        // A body-less POST skips the gate and falls through to binding, which emits the frozen
        // 400 {"body": ["is required"]} - captured on the .NET side for the same request.
        if (!hasBody(request) || !(handler instanceof HandlerMethod method)
                || !acceptsBody(method) || isJsonMediaType(request.getContentType())) {
            return true;
        }
        throw new HttpMediaTypeNotSupportedException(request.getContentType(), List.of());
    }

    public static boolean isJsonMediaType(@Nullable String contentType) {
        String type = contentType == null ? "" : contentType;
        int semicolon = type.indexOf(';');
        if (semicolon >= 0) {
            type = type.substring(0, semicolon);
        }
        type = trimSpacesAndTabs(type).toLowerCase(Locale.ROOT);
        return type.equals("application/json")
                || (type.endsWith("+json") && type.indexOf('/') >= 0);
    }

    /** {@code String.strip()} would also remove Unicode whitespace; the frozen grammar does not. */
    private static String trimSpacesAndTabs(String value) {
        int start = 0;
        int end = value.length();
        while (start < end && (value.charAt(start) == ' ' || value.charAt(start) == '\t')) {
            start++;
        }
        while (end > start && (value.charAt(end - 1) == ' ' || value.charAt(end - 1) == '\t')) {
            end--;
        }
        return value.substring(start, end);
    }

    /** Same "has a body" test as {@code MediaTypeGateMiddleware}: a declared length, or chunked. */
    private static boolean hasBody(HttpServletRequest request) {
        long length = request.getContentLengthLong();
        return length > 0 || (length < 0 && request.getHeader("Transfer-Encoding") != null);
    }

    private static boolean acceptsBody(HandlerMethod method) {
        for (MethodParameter parameter : method.getMethodParameters()) {
            if (parameter.hasParameterAnnotation(RequestBody.class)) {
                return true;
            }
        }
        return false;
    }
}
