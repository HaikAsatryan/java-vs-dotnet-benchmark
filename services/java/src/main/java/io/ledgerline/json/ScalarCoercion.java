package io.ledgerline.json;

import org.springframework.boot.jackson.autoconfigure.JsonMapperBuilderCustomizer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import tools.jackson.databind.cfg.CoercionAction;
import tools.jackson.databind.cfg.CoercionInputShape;
import tools.jackson.databind.type.LogicalType;

/**
 * The two scalar shapes {@code spring.jackson.mapper.allow-coercion-of-scalars} gets wrong against
 * {@code JsonSerializerDefaults.Web}: System.Text.Json ACCEPTS a string into a number
 * ({@code NumberHandling = AllowReadingFromString}) and REFUSES a number into a string, and the
 * blanket switch does the opposite of both. Driven against the two live services and asserted in
 * ErrorContractTest.
 */
@Configuration(proxyBeanMethods = false)
public class ScalarCoercion {

    @Bean
    JsonMapperBuilderCustomizer scalarShapesMatchSystemTextJson() {
        return builder -> builder
                .withCoercionConfig(LogicalType.Integer, config -> config
                        .setCoercion(CoercionInputShape.String, CoercionAction.TryConvert))
                .withCoercionConfig(LogicalType.Textual, config -> config
                        .setCoercion(CoercionInputShape.Integer, CoercionAction.Fail)
                        .setCoercion(CoercionInputShape.Float, CoercionAction.Fail)
                        .setCoercion(CoercionInputShape.Boolean, CoercionAction.Fail));
    }
}
