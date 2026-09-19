FROM php:8.3-apache@sha256:82c546aad03091876bfaa4dfd789767b1c462d75433991f1db38610254990f4e

# System deps: YAML, curl (healthcheck), git, unzip
RUN apt-get update && apt-get install -y \
        libyaml-dev \
        curl \
        git \
        unzip \
    && docker-php-ext-install pdo_mysql mysqli \
    && pecl install yaml \
    && docker-php-ext-enable yaml pdo_mysql mysqli \
    && rm -rf /var/lib/apt/lists/*

# Composer
COPY --from=composer:latest /usr/bin/composer /usr/bin/composer

# Apache: mod_rewrite + AllowOverride All
RUN a2enmod rewrite \
    && sed -i 's/AllowOverride None/AllowOverride All/g' /etc/apache2/apache2.conf

# Blokuj dostęp do .env przez Apache
RUN printf '<FilesMatch "^\\.env(\\..*)?$">\n    Require all denied\n</FilesMatch>\n' \
        > /etc/apache2/conf-available/env-deny.conf \
    && a2enconf env-deny

# Skopiuj pliki aplikacji (bez .env — montowany jako volume)
COPY --chown=www-data:www-data . /var/www/html/

# Zainstaluj zależności PHP
RUN if [ -f /var/www/html/composer.json ]; then \
        cd /var/www/html && composer install --no-dev --optimize-autoloader --no-interaction 2>/dev/null || true; \
    fi

# Uprawnienia do katalogu var (logi, cache, sesje)
RUN mkdir -p /var/www/html/var \
    && chown -R www-data:www-data /var/www/html/var \
    && chmod -R 750 /var/www/html/var

EXPOSE 80

CMD ["apache2-foreground"]
