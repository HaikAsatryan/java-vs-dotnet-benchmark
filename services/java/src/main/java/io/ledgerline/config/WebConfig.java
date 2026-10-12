package io.ledgerline.config;

import io.ledgerline.infrastructure.AccessLogFilter;
import io.ledgerline.infrastructure.BodySizeLimitFilter;
import io.ledgerline.infrastructure.JsonMediaTypeGate;
import io.ledgerline.infrastructure.RequestIdFilter;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import java.util.LinkedHashSet;
import java.util.Set;
import org.springframework.boot.webmvc.autoconfigure.WebMvcRegistrations;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpMethod;
import org.springframework.web.HttpRequestMethodNotSupportedException;
import org.springframework.web.bind.annotation.RequestMethod;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;
import org.springframework.web.servlet.mvc.method.RequestMappingInfo;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;

/**
 * The MVC wiring, the counterpart of the pipeline {@code services/dotnet/Program.cs} builds by
 * hand. A {@link WebMvcConfigurer} rather than {@code @EnableWebMvc}, which would switch off
 * Boot's MVC auto-configuration wholesale and take the configured Jackson converter with it.
 *
 * <p>The three filter beans run in the order {@code Program.cs} adds its middleware: request id
 * first, then the access line around everything downstream, then the body limit. Their relative
 * order is carried by {@code @Order} on each class, which is what Boot's filter registration
 * sorts on.
 */
@Configuration(proxyBeanMethods = false)
public class WebConfig implements WebMvcConfigurer {

    @Bean
    public RequestIdFilter requestIdFilter() {
        return new RequestIdFilter();
    }

    @Bean
    public AccessLogFilter accessLogFilter() {
        return new AccessLogFilter();
    }

    @Bean
    public BodySizeLimitFilter bodySizeLimitFilter(LedgerProperties properties) {
        return new BodySizeLimitFilter(properties);
    }

    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(new JsonMediaTypeGate());
    }

    /**
     * Spring answers OPTIONS on a mapped path itself: a 200, an {@code Allow} that adds the
     * implicit HEAD and OPTIONS, and an EMPTY {@code Accept-Patch} header. Minimal API maps no
     * OPTIONS handler and answers 405 with the frozen envelope, so the mismatch is routed to
     * the exception handler here instead.
     */
    @Bean
    public WebMvcRegistrations methodMismatchIsAlwaysAnError() {
        return new WebMvcRegistrations() {
            @Override
            public RequestMappingHandlerMapping getRequestMappingHandlerMapping() {
                return new RequestMappingHandlerMapping() {
                    @Override
                    protected HandlerMethod handleNoMatch(Set<RequestMappingInfo> infos,
                            String lookupPath, HttpServletRequest request) throws ServletException {
                        if (HttpMethod.OPTIONS.matches(request.getMethod())) {
                            Set<String> declared = declaredMethodsFor(infos, request);
                            if (!declared.isEmpty()) {
                                throw new HttpRequestMethodNotSupportedException(
                                        request.getMethod(), declared);
                            }
                        }
                        return super.handleNoMatch(infos, lookupPath, request);
                    }
                };
            }
        };
    }

    private static Set<String> declaredMethodsFor(Set<RequestMappingInfo> infos,
            HttpServletRequest request) {
        Set<String> methods = new LinkedHashSet<>();
        for (RequestMappingInfo info : infos) {
            if (info.getPathPatternsCondition() != null
                    && info.getPathPatternsCondition().getMatchingCondition(request) != null) {
                for (RequestMethod method : info.getMethodsCondition().getMethods()) {
                    methods.add(method.name());
                }
            }
        }
        return methods;
    }
}
