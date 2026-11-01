FROM maven:3.9.9-eclipse-temurin-17 AS build
WORKDIR /src
COPY server/pom.xml ./pom.xml
RUN mvn -B dependency:go-offline
COPY server/src ./src
RUN mvn -B -DskipTests package
FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=build /src/target/configvault-0.1.0.jar /app/app.jar
USER 10001
EXPOSE 8080
ENTRYPOINT ["java","-jar","/app/app.jar"]
