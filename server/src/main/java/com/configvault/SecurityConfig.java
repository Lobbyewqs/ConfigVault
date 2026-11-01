package com.configvault;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.core.authority.AuthorityUtils;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.core.userdetails.User;
import org.springframework.security.core.userdetails.UserDetailsService;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;
import org.springframework.security.web.csrf.CookieCsrfTokenRepository;
import org.springframework.security.web.csrf.CsrfFilter;
import org.springframework.web.filter.OncePerRequestFilter;

@Configuration
public class SecurityConfig {
    static String hash(byte[] bytes) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes)); }
        catch (Exception e) { throw new IllegalStateException(e); }
    }
    @Bean PasswordEncoder passwordEncoder() { return new BCryptPasswordEncoder(); }
    @Bean UserDetailsService users(JdbcTemplate db) {
        return username -> db.query("SELECT username,password_hash,enabled FROM app_user WHERE username=?", rs -> {
            if (!rs.next()) throw new org.springframework.security.core.userdetails.UsernameNotFoundException("not found");
            return User.withUsername(rs.getString(1)).password(rs.getString(2)).disabled(!rs.getBoolean(3)).roles("USER").build();
        }, username);
    }
    @Bean SecurityFilterChain security(HttpSecurity http, JdbcTemplate db) throws Exception {
        var filter = new OncePerRequestFilter() {
            @Override protected void doFilterInternal(HttpServletRequest req, HttpServletResponse res, FilterChain chain) throws ServletException, IOException {
                String auth = req.getHeader("Authorization");
                if (auth != null && auth.startsWith("Bearer ") && SecurityContextHolder.getContext().getAuthentication() == null) {
                    String token = auth.substring(7);
                    var users = db.queryForList("SELECT u.username FROM api_token t JOIN app_user u ON u.id=t.owner_id WHERE t.token_hash=? AND t.revoked_at IS NULL AND t.expires_at>CURRENT_TIMESTAMP(6) AND u.enabled=1", String.class, hash(token.getBytes(StandardCharsets.UTF_8)));
                    if (!users.isEmpty()) {
                        var a = new UsernamePasswordAuthenticationToken(users.get(0), null, AuthorityUtils.createAuthorityList("ROLE_USER"));
                        a.setDetails("api-token");
                        SecurityContextHolder.getContext().setAuthentication(a);
                    }
                }
                chain.doFilter(req, res);
            }
        };
        http.authorizeHttpRequests(a -> a.requestMatchers("/", "/error", "/index.html", "/app.js", "/style.css", "/api/csrf", "/api/register").permitAll().anyRequest().authenticated())
            .csrf(c -> c.csrfTokenRepository(CookieCsrfTokenRepository.withHttpOnlyFalse())
                .ignoringRequestMatchers(req -> { var a=SecurityContextHolder.getContext().getAuthentication(); return a != null && "api-token".equals(a.getDetails()); }))
            .formLogin(f -> f.loginProcessingUrl("/api/session").successHandler((req,res,a)->res.setStatus(204)).failureHandler((req,res,e)->res.sendError(401)))
            .logout(l -> l.logoutUrl("/api/logout").logoutSuccessHandler((req,res,a)->res.setStatus(204)))
            .exceptionHandling(e -> e.authenticationEntryPoint((req,res,ex)->res.sendError(401)))
            .addFilterBefore(filter, CsrfFilter.class);
        return http.build();
    }
}
