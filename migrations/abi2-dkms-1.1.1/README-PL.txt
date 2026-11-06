HP Z2 Mini G5 — migracja działającej paczki ABI 2 do DKMS
2026-11-06 | wersja pakowania 1.1.1 | kod sterownika ABI 2 bez zmian

Dla obecnej instalacji użytkownika
- HP 8755, BIOS S50 Ver. 01.04.20 i znany DSDT.
- Obecnie uruchomiony 6.8.0-147-generic, aktywna paczka ABI 2 z test27.
- Zainstalowany kernel i nagłówki 6.8.0-149-generic, stary DKMS 1.0 dla 149.
- Domyślna konfiguracja DKMS 3.0.11. Przy własnych ustawieniach instalator
  odmawia przed zmianami, aby można je było przejrzeć.

Co robi migracja
Dodaje DKMS hp_smm_fan/1.1.1 i buduje poprawiony moduł dla 149 na HP.
Budowanie ma limit dwóch zadań kompilatora. To kompilacja, nie test obciążenia.
Aktualizuje pliki modułu i initramfs wyłącznie kernela 149. Dodaje osobny
selektor klienta oraz plik 82-hp-z2-dkms.conf do konfiguracji usługi.
Wykonuje systemctl daemon-reload, bez restartowania usługi.

Działający sterownik 147, jego pliki, selektor z test27, initramfs 147,
oryginalny demon oraz /etc/hp-fan.conf pozostają niezmienione. Na kernelu 147
nowy selektor przekazuje start do znanego selektora z test27.
Nie ma poleceń WMI/EC, zmian BIOS, PWM, Turbo ani testów obciążeniowych.
Instalator nie wywołuje modprobe/rmmod, stop/restart usługi ani reboot.
Po późniejszym uruchomieniu 149 normalna usługa rozpocznie sterowanie według
Twojej obecnej krzywej, używając tego samego poprawionego kodu co na 147.

Instalacja na HP, kiedy nadal działa 147
1. Skopiuj ZIP do nowego katalogu. Nie nadpisuj test27.
   unzip HP_FAN_DKMS_MIGRATION.zip
   cd HP_FAN_DKMS_MIGRATION
   sha256sum -c SHA256SUMS

2. Przy wszystkich wynikach OK przygotuj migrację.
   sudo /usr/bin/python3 -IB migrate.py stage

   To przygotowuje pliki do kolejnego uruchomienia systemu. Nie uruchamiaj
   równocześnie apt, aktualizacji kernela ani instalatora z GitHuba/test27.
   Sukces musi zakończyć się trzema komunikatami:
   STAGED_FOR_149_REBOOT
   RUNNING_147_UNCHANGED
   CURVE_UNCHANGED

   Przy STOP nie ponawiaj stage, nie restartuj usługi i nie uruchamiaj 149.
   Zachowaj cały wynik. Dziennik i stan odzyskiwania są w:
   /var/lib/hp-z2-fan-dkms-migration-1.1.1/
   Nie kasuj tego katalogu. Procedura rollback jest poniżej.

3. Dopiero po STAGED_FOR_149_REBOOT, w dogodnym momencie, wykonaj normalny
   restart serwera. W GRUB wybierz 6.8.0-149-generic, jeżeli nie jest domyślny.
   sudo systemctl reboot

4. Po uruchomieniu wróć do katalogu paczki i sprawdź:
   uname -r
   sudo /usr/bin/python3 -IB migrate.py status
   dkms status

   Oczekiwane: kernel 6.8.0-149-generic, status DKMS_ABI2_ACTIVE,
   control_abi 2, hp_smm_fan/1.1.1 dla 149 w stanie installed.
   minimum_command_cache oznacza zaakceptowane polecenie, nie pomiar RPM.
   Status nie wykonuje poleceń firmware ani pomiarów wentylatora.
   Jeżeli nadal jest 147, ABI2_147_ACTIVE oznacza działanie wcześniejszej
   poprawionej wersji. Nie jest to dowód uruchomienia nowej wersji na 149.

Odmowa / błąd po starcie 149
Nie wymuszaj uruchomienia usługi i nie usuwaj zabezpieczeń. Zachowaj:
   sudo journalctl -b -u hp-fan.service -n 60 --no-pager
   sudo /usr/bin/python3 -IB migrate.py status
Przy nieznanym wyniku firmware (cache -2 / timeout SMM) nie przeładowuj
modułu ani nie restartuj wyłącznie po to, aby skasować błąd. Zachowaj log.
Przy zwykłym błędzie kwalifikacji modułu/klienta wróć przy planowanym
uruchomieniu do zachowanego kernela 147 w Advanced options for Ubuntu.

Wycofanie migracji
Najpierw uruchom znany kernel 147. W tym stanie usługa ma używać pierwotnej
paczki ABI 2 z test27. Nie uruchamiaj rollback z test27 — to inna instalacja.
W nowej paczce:
   sudo /usr/bin/python3 -IB migrate.py rollback

Ta operacja usuwa DKMS 1.1.1, ponownie instaluje znany DKMS 1.0 dla 149,
aktualizuje initramfs 149 i usuwa wyłącznie nowy dodatek/selektor/źródła.
Pozostawia działającą paczkę ABI 2 na 147, krzywą oraz dzienniki.
Oczekiwany wynik ROLLED_BACK_TO_147_BUNDLE. Dalszą pracę z poprawką prowadź
wtedy na 147; na 149 znów znajdzie się stary sterownik 1.0.
Wycofanie odmawia, jeśli 1.1.1 zdążyło obsłużyć dodatkowy kernel poza 149,
jeżeli pliki zostały zmienione lub stan firmware jest nieznany. Potrzebny
jest wtedy przegląd stanu, bez automatycznego kasowania nowych instalacji.
Kernel 147 stanowi drogę powrotu — zachowaj go podczas wdrożenia.

Kolejne aktualizacje
AUTOINSTALL dla 1.1.1 obsługuje kolejne kernele 6.8.0-N-generic, N >=149,
x86_64. Przy błędzie budowania DKMS nie ma gwarancji działania na nowym
kernelu. Sprawdzaj wynik aktualizacji i po restarcie migrate.py status.
147 jest jawnie wykluczony z tej nowej paczki DKMS i zachowuje własny moduł.
Przejście na inną serię (np. HWE), zmianę BIOS, Secure Boot/lockdown lub
własne ustawienia DKMS należy osobno sprawdzić. Niczego takiego nie zmieniamy.
Nie należy równolegle używać install.sh z repozytorium do tej instalacji.

Zakres sprawdzenia
Budowanie rzeczywistego modułu dla 149 przez make i DKMS wykonano w VM,
bez ładowania modułu. Testy migracji wykonano na atrapach plików i poleceń.
Pełnej instalacji i startu nowego kernela na HP nie wykonano.
Zgodność kompilacji nie dowodzi skuteczności chłodzenia pod obciążeniem.
Źródło i daemon są identyczne z wersją działającą obecnie na 147.

Paczka zawiera źródła, testy offline, raport i manifest. Zachowaj ZIP oraz
katalog stanu migracji do czasu potwierdzenia działania i wycofania.
