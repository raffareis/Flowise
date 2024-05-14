import { MigrationInterface, QueryRunner } from 'typeorm'

export class AddNameToChatHistory1711637331047 implements MigrationInterface {
    public async up(queryRunner: QueryRunner): Promise<void> {
        await queryRunner.query('ALTER TABLE `chat_message` ADD COLUMN `chatType` VARCHAR(255)')
    }

    public async down(queryRunner: QueryRunner): Promise<void> {
        await queryRunner.query(`ALTER TABLE "chat_message" DROP COLUMN "name";`)
    }
}
