package com.configvault;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
@MapperScan("com.configvault")
public class ConfigVaultApplication {
    public static void main(String[] args) { SpringApplication.run(ConfigVaultApplication.class, args); }
}
