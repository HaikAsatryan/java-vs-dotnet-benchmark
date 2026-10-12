package io.ledgerline.problem;

import io.ledgerline.infrastructure.RequestContext;
import io.ledgerline.logging.LedgerLog;
import jakarta.servlet.http.HttpServletRequest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.validation.BindingResult;
import org.springframework.validation.FieldError;
import org.springframework.web.HttpMediaTypeNotSupportedException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.context.request.RequestAttributes;
import org.springframework.web.context.request.ServletWebRequest;
import org.springframework.web.context.request.WebRequest;
import org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler;

/**
 * Every error body this service emits, rewritten to the frozen envelopes of spec/openapi.yaml.
 * Counterpart of {@code ProblemDetailsConfig.cs}, which rewrites every ProblemDetails ASP.NET
 * produces rather than only the ones the app raises - {@link #createResponseEntity} does that job
 * for the exception types {@link ResponseEntityExceptionHandler} covers.
 *
 * <p>The hooks are the PROTECTED ones: a sibling {@code @ExceptionHandler} for an exception the
 * base class already maps fails the context at startup ({@code handleException} is
 * {@code public final}). Without the two overrides below, an unreadable body and a wrong content
 * type return EMPTY - Boot 4 produces no ProblemDetail for either unless
 * {@code spring.mvc.problemdetails.enabled} is on.
 */
@RestControllerAdvice
public class LedgerExceptionHandler extends ResponseEntityExceptionHandler {

    private static final Logger LOGGER = LoggerFactory.getLogger(LedgerExceptionHandler.class);

    /**
     * The annotations whose failure suppresses the other constraints on the same field.
     * {@code Validator.GetValidationErrors} tests {@code [Required]} first and RETURNS on failure;
     * Jakarta has no such rule, and {@code @Pattern}/{@code @Size} do fire on the empty string.
     * A {@code @GroupSequence} is the wrong tool - it aborts the whole bean, not the one member.
     */
    private static final Set<String> PRESENCE_CONSTRAINTS = Set.of("NotNull", "NotBlank");

    @ExceptionHandler(NotFoundException.class)
    public ResponseEntity<Problem> handleNotFound(NotFoundException ex) {
        return respond(Problem.notFound());
    }

    @ExceptionHandler(ValidationFailedException.class)
    public ResponseEntity<Problem> handleValidationFailed(ValidationFailedException ex) {
        return respond(Problem.validation(ex.errors()));
    }

    /**
     * Anything the base class does not map. Two lines, in this order, so the 500-path log
     * streams are line-for-line identical across the stacks rather than merely set-equal.
     * Field-level 400s and every 404 above log NOTHING - spec/logging.md records those through
     * the access line's status alone.
     */
    @ExceptionHandler(Exception.class)
    public ResponseEntity<Problem> handleUnexpected(Exception ex, HttpServletRequest request) {
        String requestId = RequestContext.requestId(request);
        LedgerLog.requestError(LOGGER, 500, "internal", requestId);
        LedgerLog.exception(LOGGER, String.valueOf(ex.getMessage()), requestId);
        return respond(Problem.internal());
    }

    @Override
    protected ResponseEntity<Object> handleMethodArgumentNotValid(
            MethodArgumentNotValidException ex, HttpHeaders headers, HttpStatusCode status,
            WebRequest request) {
        return handleExceptionInternal(
                ex, Problem.validation(errorsOf(ex.getBindingResult())), headers, status, request);
    }

    @Override
    protected ResponseEntity<Object> handleHttpMessageNotReadable(
            HttpMessageNotReadableException ex, HttpHeaders headers, HttpStatusCode status,
            WebRequest request) {
        // The oversized body arrives here too - the limit trips inside the converter's read - and
        // is told apart by the request attribute, not by a cause chain the converter may rewrap.
        // spec/logging.md gives the 413 an access line and NO request_error, hence two branches.
        if (Boolean.TRUE.equals(
                request.getAttribute(PayloadTooLargeException.REQUEST_ATTRIBUTE,
                        RequestAttributes.SCOPE_REQUEST))) {
            return handleExceptionInternal(ex, Problem.payloadTooLarge(), headers,
                    HttpStatus.PAYLOAD_TOO_LARGE, request);
        }
        // Malformed, undeserializable or absent body: the frozen {"body": ["is required"]},
        // captured identically from the .NET SUT for both. One of the exactly two paths
        // spec/logging.md gives a request_error(validation) to.
        LedgerLog.requestError(LOGGER, 400, "validation", requestId(request));
        Map<String, List<String>> errors =
                Map.of("body", List.of(ValidationStrings.REQUIRED));
        return handleExceptionInternal(ex, Problem.validation(errors), headers, status, request);
    }

    @Override
    protected ResponseEntity<Object> handleHttpMediaTypeNotSupported(
            HttpMediaTypeNotSupportedException ex, HttpHeaders headers, HttpStatusCode status,
            WebRequest request) {
        // The second and last request_error(validation) path.
        LedgerLog.requestError(LOGGER, 415, "validation", requestId(request));
        // Fresh headers: the inherited implementation advertises Accept (and sometimes
        // Accept-Patch) on the 415, which the .NET response does not carry.
        return handleExceptionInternal(
                ex, Problem.unsupportedMediaType(), new HttpHeaders(), status, request);
    }

    /**
     * The single exit for every body {@link ResponseEntityExceptionHandler} produces: anything
     * that did not arrive as a {@link Problem} is replaced by the frozen envelope for its status.
     */
    @Override
    protected ResponseEntity<Object> createResponseEntity(
            Object body, HttpHeaders headers, HttpStatusCode statusCode, WebRequest request) {
        Object frozen = body instanceof Problem ? body : Problem.forStatus(statusCode.value());
        HttpHeaders out = new HttpHeaders();
        out.putAll(headers);
        out.setContentType(MediaType.APPLICATION_PROBLEM_JSON);
        return super.createResponseEntity(frozen, out, statusCode, request);
    }

    /** The base class hands out a {@link WebRequest}; the id lives on the servlet request. */
    private static String requestId(WebRequest request) {
        return request instanceof ServletWebRequest servlet
                ? RequestContext.requestId(servlet.getRequest())
                : "";
    }

    private static ResponseEntity<Problem> respond(Problem problem) {
        return ResponseEntity.status(problem.status())
                .contentType(MediaType.APPLICATION_PROBLEM_JSON)
                .body(problem);
    }

    private static Map<String, List<String>> errorsOf(BindingResult binding) {
        Map<String, List<String>> errors = new LinkedHashMap<>();
        Set<String> presenceFailed = new HashSet<>();
        for (FieldError error : binding.getFieldErrors()) {
            String key = FieldPath.toClrPath(error.getField());
            if (PRESENCE_CONSTRAINTS.contains(constraintOf(error))) {
                // Wins regardless of the order Hibernate Validator returns its violation set in.
                errors.put(key, new ArrayList<>(List.of(error.getDefaultMessage())));
                presenceFailed.add(key);
            } else if (!presenceFailed.contains(key)) {
                errors.computeIfAbsent(key, k -> new ArrayList<>())
                        .add(error.getDefaultMessage());
            }
        }
        return errors;
    }

    /**
     * The bare annotation name: the last, least-qualified code DefaultMessageCodesResolver emits.
     */
    private static String constraintOf(FieldError error) {
        List<String> codes = Arrays.asList(error.getCodes());
        return codes.getLast();
    }
}
